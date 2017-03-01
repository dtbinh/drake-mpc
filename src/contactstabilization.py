from __future__ import absolute_import, division, print_function

from itertools import islice, chain
import numpy as np
import pydrake.solvers.mathematicalprogram as mp
from pydrake.solvers.gurobi import GurobiSolver
from polynomial import Polynomial
from piecewise import Piecewise
from trajectory import Trajectory
from boxatlas import BoxAtlasState, BoxAtlasInput


class MixedIntegerTrajectoryOptimization(mp.MathematicalProgram):
    def add_continuity_constraints(self, piecewise):
        for t in piecewise.breaks[1:-1]:
            frombelow = piecewise.from_below(t)
            fromabove = piecewise.from_above(t)
            for i in range(frombelow.size):
                self.AddLinearConstraint(frombelow.flat[i] == fromabove.flat[i])

    def new_piecewise_polynomial_variable(self, domain, dimension, degree, kind="continuous"):
        if kind == "continuous":
            C = [self.NewContinuousVariables(degree + 1, len(domain) - 1) for _ in range(dimension)]
        elif kind == "binary":
            C = [self.NewBinaryVariables(degree + 1, len(domain) - 1) for _ in range(dimension)]
        else:
            raise ValueError("Expected kind to be 'continuous' or 'binary', but got {:s}".format(kind))
        C = np.stack(C, axis=2)
        assert C.shape == (degree + 1, len(domain) - 1, dimension)
        return Piecewise(domain,
                         [Polynomial([C[i, j, :] for i in range(degree + 1)])
                              for j in range(len(domain) - 1)])

    def add_limb_velocity_constraints(self, qcom, qlimb, vmax, dt):
        ts = qcom.breaks
        dim = qcom(ts[0]).size
        vcom = qcom.derivative()
        for j in range(len(ts) - 2):
            relative_velocity = 1.0 / dt * (qlimb(ts[j + 1]) - qlimb(ts[j])) - vcom(ts[j])
            for i in range(dim):
                self.AddLinearConstraint((relative_velocity[i] - vmax) <= 0)
                self.AddLinearConstraint((-relative_velocity[i] - vmax) <= 0)

    def add_dynamics_constraints(self, robot, qcom, contact_force):
        dim = robot.dim
        num_limbs = len(robot.limb_bounds)
        gravity_force = np.zeros(dim)
        gravity_force[-1] = -robot.mass * robot.g
        ts = qcom.breaks
        acom = qcom.derivative().derivative()
        for t in ts[:-1]:
            for i in range(dim):
                total_contact_force = sum([contact_force[k](t)[i] for k in range(num_limbs)])
                self.AddLinearConstraint(total_contact_force + gravity_force[i] == robot.mass * acom(t)[i])

    def add_no_force_at_distance_constraints(self, contact, contact_force, Mbig):
        ts = contact.breaks
        dim = contact_force(ts[0]).size
        for t in ts[:-1]:
            for i in range(dim):
                self.AddLinearConstraint((contact_force(t)[i] - (Mbig * contact(t)[0])) <= 0)
                self.AddLinearConstraint((-contact_force(t)[i] - (Mbig * contact(t)[0])) <= 0)

    def add_contact_surface_constraints(self, qlimb, surface, contact, Mbig):
        ts = qlimb.breaks
        for j in range(len(ts) - 1):
            t = ts[j]
            A = surface.pose_constraints.getA()
            b = surface.pose_constraints.getB()
            qlimb_after_dt = qlimb.from_below(ts[j + 1])
            for i in range(A.shape[0]):
                self.AddLinearConstraint((A[i, :].dot(qlimb_after_dt) - (b[i] + Mbig * (1 - contact(t)[0]))) <= 0)

    def add_contact_force_constraints(self, contact_force, surface, contact, Mbig):
        ts = contact_force.breaks
        for t in ts[:-1]:
            A = surface.force_constraints.getA()
            b = surface.force_constraints.getB()
            for i in range(A.shape[0]):
                self.AddLinearConstraint((A[i, :].dot(contact_force(t)) - (b[i] + Mbig * (1 - contact(t)[0]))) <= 0)

    def add_contact_velocity_constraints(self, qlimb, contact, Mbig):
        ts = qlimb.breaks
        dim = qlimb(0).size

        vlimb = qlimb.derivative()
        for j in range(len(ts) - 2):
            t = ts[j]
            tnext = ts[j + 1]
            indicator = contact(t)[0]
            for i in range(dim):
                self.AddLinearConstraint((vlimb(tnext)[i] - (Mbig * (1 - indicator))) <= 0)
                self.AddLinearConstraint((-vlimb(tnext)[i] - (Mbig * (1 - indicator))) <= 0)

    def get_piecewise_solution(self, piecewise):
        return piecewise.map(lambda p: p.map(lambda x: self.GetSolution(x)))

    def extract_solution(self, robot, qcom, qlimb, contact, contact_force):
        qcom = self.get_piecewise_solution(qcom)
        vcom = qcom.map(Polynomial.derivative)
        qlimb = [self.get_piecewise_solution(q) for q in qlimb]
        flimb = [self.get_piecewise_solution(f) for f in contact_force]
        contact = [self.get_piecewise_solution(c) for c in contact]
        return (Trajectory(
            [qcom, vcom] + qlimb,
            lambda qcom, vcom, *qlimb: BoxAtlasState(robot, qcom=qcom, vcom=vcom, qlimb=qlimb)
        ), Trajectory(
            flimb,
            lambda *flimb: BoxAtlasInput(robot, flimb=flimb)
        ), contact)

    def add_kinematic_constraints(self, qlimb, qcom, polytope):
        A = polytope.getA()
        b = polytope.getB()
        for (ql, qc) in islice(zip(qlimb.at_all_breaks(), qcom.at_all_breaks()), 1, None):
            offset = ql - qc
            for i in range(A.shape[0]):
                self.AddLinearConstraint((A[i, :].dot(offset) - b[i]) <= 0)

    def count_contact_switches(self, contact):
        ts = contact.breaks
        delta = self.NewContinuousVariables(len(ts) - 2, "contact_delta")
        for j in range(len(ts) - 2):
            self.AddLinearConstraint(contact(ts[j + 1])[0] - contact(ts[j])[0] <= delta[j])
            self.AddLinearConstraint(-(contact(ts[j + 1])[0] - contact(ts[j])[0]) <= delta[j])
        total_switches = self.NewContinuousVariables(1, "contact_switches")[0]
        self.AddLinearConstraint(sum(delta) <= total_switches)
        return total_switches


class BoxAtlasVariables(object):
    def __init__(self, prog, ts, num_limbs, dim):
        self.qcom = prog.new_piecewise_polynomial_variable(ts, dim, 2)
        prog.add_continuity_constraints(self.qcom)
        self.vcom = self.qcom.derivative()
        prog.add_continuity_constraints(self.vcom)

        self.qlimb = [prog.new_piecewise_polynomial_variable(ts, dim, 1) for k in range(num_limbs)]
        self.vlimb = [q.derivative() for q in self.qlimb]
        for q in self.qlimb:
            prog.add_continuity_constraints(q)
        self.contact_force = [prog.new_piecewise_polynomial_variable(ts, dim, 0) for k in range(num_limbs)]
        self.contact = [prog.new_piecewise_polynomial_variable(ts, 1, 0, kind="binary") for k in range(num_limbs)]


class BoxAtlasContactStabilization(object):
    def __init__(self, initial_state, env,
                 dt=0.05,
                 num_time_steps=20):
        self.robot = initial_state.robot
        time_horizon = num_time_steps * dt
        self.dt = dt
        self.ts = np.linspace(0, time_horizon, time_horizon / dt + 1)
        self.dim = self.robot.dim

        self.env = env
        self.prog = MixedIntegerTrajectoryOptimization()
        num_limbs = len(self.robot.limb_bounds)
        self.vars = BoxAtlasVariables(self.prog, self.ts, num_limbs, self.dim)
        self.add_constraints(initial_state)
        self.add_costs()

    def add_constraints(self, initial_state, vlimb_max=5, Mq=10, Mv=100, Mf=1000):
        num_limbs = len(self.robot.limb_bounds)
        for k in range(num_limbs):
            self.prog.add_no_force_at_distance_constraints(self.vars.contact[k],
                                                           self.vars.contact_force[k], Mf)
            self.prog.add_contact_surface_constraints(self.vars.qlimb[k],
                                                      self.env.surfaces[k],
                                                      self.vars.contact[k], Mq)
            self.prog.add_contact_force_constraints(self.vars.contact_force[k],
                                                    self.env.surfaces[k],
                                                    self.vars.contact[k], Mf)
            self.prog.add_contact_velocity_constraints(self.vars.qlimb[k],
                                                       self.vars.contact[k], Mv)
            self.prog.add_limb_velocity_constraints(self.vars.qcom,
                                                    self.vars.qlimb[k],
                                                    vlimb_max,
                                                    self.dt)
            self.prog.add_kinematic_constraints(self.vars.qlimb[k],
                                                self.vars.qcom,
                                                self.robot.limb_bounds[k])
            # switches = self.prog.count_contact_switches(self.vars.contact[k])
            # self.prog.AddLinearConstraint(switches <= 2)

        A = self.env.free_space.getA()
        b = self.env.free_space.getB()
        for q in chain(self.vars.qcom.at_all_breaks(),
                       *[ql.at_all_breaks() for ql in self.vars.qlimb]):
            for i in range(A.shape[0]):
                self.prog.AddLinearConstraint((A[i, :].dot(q) - b[i]) <= 0)

        self.prog.add_dynamics_constraints(self.robot, self.vars.qcom, self.vars.contact_force)

        for i in range(self.dim):
            self.prog.AddLinearConstraint(self.vars.qcom(self.ts[0])[i] == initial_state.qcom[i])
            self.prog.AddLinearConstraint(self.vars.vcom(self.ts[0])[i] == initial_state.vcom[i])
            for k in range(num_limbs):
                self.prog.AddLinearConstraint(self.vars.vlimb[k](self.ts[0])[i] == 0)
                self.prog.AddLinearConstraint(self.vars.qlimb[k](self.ts[0])[i] == initial_state.qlimb[k][i])

    def add_costs(self):
        num_limbs = len(self.robot.limb_bounds)
        self.prog.AddQuadraticCost(
            0.001 * np.sum(np.sum(np.power(self.vars.contact_force[k](t), 2)) for t in self.ts[:-1] for k in range(num_limbs)))
        self.prog.AddQuadraticCost(
            100 * np.sum(np.sum(np.power(q - np.array([0, 1]), 2)) for q in self.vars.qcom.at_all_breaks()))
        self.prog.AddQuadraticCost(
            100 * np.sum(np.power(self.vars.qcom.from_below(self.ts[-1]) - np.array([0, 1]), 2)))
        self.prog.AddQuadraticCost(
            100 * np.sum(10 * np.power(self.vars.vcom.from_below(self.ts[-1]) - np.array([0, 0]), 2)))

        qcomf = self.vars.qcom.from_below(self.ts[-1])
        qlimbf = [self.vars.qlimb[k].from_below(self.ts[-1]) for k in range(num_limbs)]
        self.prog.AddQuadraticCost(1000 * (qlimbf[1][0] - (qcomf[0] + 0.25))**2)
        self.prog.AddQuadraticCost(1000 * (qlimbf[2][0] - (qcomf[0] - 0.25))**2)

    def solve(self):
        solver = GurobiSolver()
        self.prog.SetSolverOption(mp.SolverType.kGurobi, "LogToConsole", 1)
        self.prog.SetSolverOption(mp.SolverType.kGurobi, "OutputFlag", 1)
        result = solver.Solve(self.prog)
        print(result)
        assert result == mp.SolutionResult.kSolutionFound
        return self.prog.extract_solution(self.robot,
                                          self.vars.qcom,
                                          self.vars.qlimb,
                                          self.vars.contact,
                                          self.vars.contact_force)
