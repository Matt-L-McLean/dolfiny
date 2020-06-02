#!/usr/bin/env python3

import numpy as np
from petsc4py import PETSc
from mpi4py import MPI

import dolfinx
import ufl

import dolfiny.io
import dolfiny.mesh
import dolfiny.utils
import dolfiny.function
import dolfiny.snesblockproblem

import mesh_curve3d_gmshapi as mg

# Basic settings
name = "beam_static"
comm = MPI.COMM_WORLD

# Geometry and mesh parameters
L = 1.0  # beam length
N = 3 * 4  # number of nodes
p = 3  # physics: polynomial order
q = 3  # geometry: polynomial order

# Create the regular mesh of an annulus with given dimensions
gmsh_model, tdim = mg.mesh_curve3d_gmshapi(name, shape="f_arc", L=L, nL=N, order=q)

# # Create the regular mesh of an annulus with given dimensions and save as msh, then read into gmsh model
# mg.mesh_curve3d_gmshapi(name, shape="xline", L=L, nL=N, order=g, msh_file=f"{name}.msh")
# gmsh_model, tdim = dolfiny.mesh.msh_to_gmsh(f"{name}.msh")

# Get mesh and meshtags
mesh, mts = dolfiny.mesh.gmsh_to_dolfin(gmsh_model, tdim)

# # Write mesh and meshtags to file
# with dolfiny.io.XDMFFile(comm, f"{name}.xdmf", "w") as ofile:
#     ofile.write_mesh_meshtags(mesh, mts)

# # Read mesh and meshtags from file
# with dolfiny.io.XDMFFile(comm, f"{name}.xdmf", "r") as ifile:
#     mesh, mts = ifile.read_mesh_meshtags()

# Get merged MeshTags for each codimension
subdomains, subdomains_keys = dolfiny.mesh.merge_meshtags(mts, tdim - 0)
interfaces, interfaces_keys = dolfiny.mesh.merge_meshtags(mts, tdim - 1)

# Define shorthands for labelled tags
beg = interfaces_keys["beg"]
end = interfaces_keys["end"]

# Structure material and load parameters
E = 1.0e+8  # [N/m^2]
G = E / 2.  # [N/m^2]
A = 1.0e-4  # [m^2]
I = 1.0e-6  # [m^4]   # noqa: E741
EA = dolfinx.Constant(mesh, E * A)  # axial stiffness
EI = dolfinx.Constant(mesh, E * I)  # bending stiffness
GA = dolfinx.Constant(mesh, G * A)  # shear stiffness

μ = dolfinx.Constant(mesh, 1.0)  # load factor

p_1 = dolfinx.Constant(mesh, 1.0 * 0)
p_3 = dolfinx.Constant(mesh, 1.0 * 0)
m_2 = dolfinx.Constant(mesh, 1.0 * 0)
F_1 = dolfinx.Constant(mesh, (2.0 * np.pi / L)**2 * EI.value * 0)
F_3 = dolfinx.Constant(mesh, (0.5 * np.pi / L)**2 * EI.value * 0)
M_2 = dolfinx.Constant(mesh, (2.0 * np.pi / L)**1 * EI.value * 0)

# Define integration measures
dx = ufl.Measure("dx", domain=mesh, subdomain_data=subdomains)  # , metadata={"quadrature_degree": 4})
ds = ufl.Measure("ds", domain=mesh, subdomain_data=interfaces)  # , metadata={"quadrature_degree": 4})

# Function spaces
Ue = ufl.FiniteElement("CG", mesh.ufl_cell(), p)
We = ufl.FiniteElement("CG", mesh.ufl_cell(), p)
Re = ufl.FiniteElement("CG", mesh.ufl_cell(), p)

U = dolfinx.FunctionSpace(mesh, Ue)
W = dolfinx.FunctionSpace(mesh, We)
R = dolfinx.FunctionSpace(mesh, Re)

# Define functions
u = dolfinx.Function(U, name='u')
w = dolfinx.Function(W, name='w')
r = dolfinx.Function(R, name='r')

δu = ufl.TestFunction(U)
δw = ufl.TestFunction(W)
δr = ufl.TestFunction(R)

# Define state as (ordered) list of functions
m = [u, w, r]
δm = [δu, δw, δr]

# Coordinates of undeformed configuration
x0 = ufl.SpatialCoordinate(mesh)

# CURVATURE TENSOR
# Jacobi matrix of map reference -> undeformed
J0 = ufl.geometry.Jacobian(mesh)
iJ0 = ufl.geometry.JacobianInverse(mesh)
detJ0 = ufl.geometry.JacobianDeterminant(mesh)
# Metric tensor
G0 = ufl.dot(J0.T, J0)
# Contravariant basis
K0 = J0 * ufl.inv(G0)
# Tangent basis
g0 = J0[:, 0]
g1 = dolfinx.Constant(mesh, (0, 1, 0))  # unit vector e2 (assume curve in x-z plane)
g2 = ufl.cross(g0, g1)
# Unit tangent basis
g0 /= ufl.sqrt(ufl.dot(g0, g0))
g1 /= ufl.sqrt(ufl.dot(g1, g1))
g2 /= ufl.sqrt(ufl.dot(g2, g2))
# Curvature tensor
from ufl.differentiation import ReferenceGrad
B0 = ufl.dot(g2, ReferenceGrad(K0))  # == -ufl.dot(ReferenceGrad(g2).T, K0)
# Curvature in the undeformed configuration
κ0 = -B0[0, 0]  # TODO: check use of sign in derivation

print(f"κ0(beg) = {dolfinx.fem.assemble_scalar(κ0 * ds(beg)):7.5f}")
print(f"κ0(end) = {dolfinx.fem.assemble_scalar(κ0 * ds(end)):7.5f}")

# DERIVATIVE with respect to straight reference configuration parameterised by arc-length
GRAD = lambda u: detJ0 * ufl.dot(iJ0[0, :], ufl.grad(u))  # noqa: E731

# Stretches at the principal axis
λs = (1.0 + GRAD(x0[0]) * GRAD(u) + GRAD(x0[2]) * GRAD(w)) * ufl.cos(r) + \
     (      GRAD(x0[2]) * GRAD(u) - GRAD(x0[0]) * GRAD(w)) * ufl.sin(r)  # noqa: E201
λξ = (1.0 + GRAD(x0[0]) * GRAD(u) + GRAD(x0[2]) * GRAD(w)) * ufl.sin(r) - \
     (      GRAD(x0[2]) * GRAD(u) - GRAD(x0[0]) * GRAD(w)) * ufl.cos(r)  # noqa: E201

λ0 = ufl.sqrt(ufl.dot(GRAD(x0), GRAD(x0)))  # undeformed stretch (= 1)
# λ  = ufl.sqrt(λs**2 + λξ**2)  # deformed stretch

# Curvature
κ = -GRAD(r)
κ0 = dolfinx.Constant(mesh, -2 * np.pi)

# Green-Lagrange strains (prescribed)
λp = dolfinx.Constant(mesh, 2 / 5)  # prescribed axial stretch: 4/5, 2/3, 1/2, 2/5, 1/3 and 4/3, 2, 4
ep = μ * 1 / 2 * (λp**2 - λ0**2) * 0

# Green-Lagrange strains
e = 1 / 2 * (λs**2 + λξ**2 - λ0**2) - ep
g = λξ
k = λs * κ + (λs - λ0) * κ0

δe = ufl.derivative(e, u, δu) + ufl.derivative(e, w, δw) + ufl.derivative(e, r, δr)
δg = ufl.derivative(g, u, δu) + ufl.derivative(g, w, δw) + ufl.derivative(g, r, δr)
δk = ufl.derivative(k, u, δu) + ufl.derivative(k, w, δw) + ufl.derivative(k, r, δr)

δe = ufl.algorithms.apply_algebra_lowering.apply_algebra_lowering(δe)
δg = ufl.algorithms.apply_algebra_lowering.apply_algebra_lowering(δg)
δk = ufl.algorithms.apply_algebra_lowering.apply_algebra_lowering(δk)

δe = ufl.algorithms.apply_derivatives.apply_derivatives(δe)
δg = ufl.algorithms.apply_derivatives.apply_derivatives(δg)
δk = ufl.algorithms.apply_derivatives.apply_derivatives(δk)

# Constitutive relations (Saint-Venant Kirchhoff)
N = EA * e
T = GA * g
M = EI * k

# Weak form: components (as one-form)
F = - δe * N * dx - δg * T * dx - δk * M * dx \
    + μ * (δu * p_1 * dx + δw * p_3 * dx + δr * m_2 * dx) \
    + μ * (δu * F_1 * ds(end) + δw * F_3 * ds(end) + δr * M_2 * ds(end))

# LINEARISATION
# # Generate 1st order Taylor series of form at given state (u0, w0, r0)
# u0 = dolfinx.Function(U, name='u0')
# w0 = dolfinx.Function(W, name='w0')
# r0 = dolfinx.Function(R, name='r0')
# from ufl.algorithms.replace import Replacer
# replacer = Replacer({u: u0, w: w0, r: r0})
# from ufl.algorithms.map_integrands import map_integrand_dags
# F0 = map_integrand_dags(replacer, F)
# dF0 = ufl.derivative(F0, u0, u) + ufl.derivative(F0, w0, w) + ufl.derivative(F0, r0, r)
# dF0 -= ufl.derivative(F0, u0, u0) + ufl.derivative(F0, w0, w0) + ufl.derivative(F0, r0, r0)
# dF0 = ufl.algorithms.apply_algebra_lowering.apply_algebra_lowering(dF0)
# dF0 = ufl.algorithms.apply_derivatives.apply_derivatives(dF0)
# F = F0 + dF0

# Overall form (as list of forms)
F = dolfiny.function.extract_blocks(F, δm)

# Create output xdmf file -- open in Paraview with Xdmf3ReaderT
ofile = dolfiny.io.XDMFFile(comm, f"{name}.xdmf", "w")
# Write mesh, meshtags
# ofile.write_mesh_meshtags(mesh, mts)

# Options for PETSc backend
opts = PETSc.Options()

opts["snes_type"] = "newtonls"
opts["snes_linesearch_type"] = "basic"
opts["snes_rtol"] = 1.0e-05
opts["snes_stol"] = 1.0e-06
opts["snes_max_it"] = 12
opts["ksp_type"] = "preonly"
opts["pc_type"] = "lu"
opts["pc_factor_mat_solver_type"] = "mumps"
opts["mat_mumps_icntl_14"] = 500
opts["mat_mumps_icntl_24"] = 1

# Create nonlinear problem: SNES
problem = dolfiny.snesblockproblem.SNESBlockProblem(F, m, opts=opts)

# Identify dofs of function spaces associated with tagged interfaces/boundaries
left_dofs_U = dolfiny.mesh.locate_dofs_topological(U, interfaces, beg)
left_dofs_W = dolfiny.mesh.locate_dofs_topological(W, interfaces, beg)
left_dofs_R = dolfiny.mesh.locate_dofs_topological(R, interfaces, beg)

u_left = dolfinx.Function(U)
w_left = dolfinx.Function(W)
r_left = dolfinx.Function(R)

# Process load steps
for factor in np.linspace(0, 1, num=200 + 1):

    # Set current time
    μ.value = factor

    # Set/update boundary conditions
    problem.bcs = [
        dolfinx.fem.DirichletBC(u_left, left_dofs_U),  # disp1 left
        dolfinx.fem.DirichletBC(w_left, left_dofs_W),  # disp3 left
        dolfinx.fem.DirichletBC(r_left, left_dofs_R),  # rota2 left
    ]

    dolfiny.utils.pprint(f"\n+++ Processing load factor μ = {μ.value:5.4f}")

    # Solve nonlinear problem
    m = problem.solve()

    # Assert convergence of nonlinear solver
    assert problem.snes.getConvergedReason() > 0, "Nonlinear solver did not converge!"

    print(f"L0 = {dolfinx.fem.assemble_scalar(ufl.sqrt(λ0**2) * dx):+7.5f}")
    print(f"L  = {dolfinx.fem.assemble_scalar(ufl.sqrt(λs**2 + λξ**2) * dx):+7.5f}")

    print(f"ep = {dolfinx.fem.assemble_scalar(ep * dx):+7.5f}")
    print(f"e  = {dolfinx.fem.assemble_scalar(e * dx):+7.5f}")
    print(f"g  = {dolfinx.fem.assemble_scalar(g * dx):+7.5f}")
    print(f"k  = {dolfinx.fem.assemble_scalar(k * dx):+7.5f}")
    print(f"κ  = {dolfinx.fem.assemble_scalar(κ * dx):+7.5f}")
    print(f"κ0 = {dolfinx.fem.assemble_scalar(κ0 * dx):+7.5f}")

# Extract solution
u_, w_, r_ = m

# u_.vector.view()
# w_.vector.view()
# r_.vector.view()

# Write output
# ofile.write_function(u_)
# ofile.write_function(w_)
# ofile.write_function(r_)

ofile.close()

# Extract mesh geometry nodal coordinates
dm = mesh.geometry.dofmap
oq = [0] + [*range(2, q + 1)] + [1]  # reorder lineX nodes: all ducks in a row...
x0_idx = [[dm.links(i).tolist()[k] for k in oq] for i in range(dm.num_nodes)]
x0_idx = [item for sublist in x0_idx for item in sublist]
x0 = mesh.geometry.x[x0_idx]

# Interpolate solution at mesh geometry nodes
import dolfiny.interpolation
Q = dolfinx.FunctionSpace(mesh, ("P", q))
u__ = dolfinx.Function(Q)
w__ = dolfinx.Function(Q)
r__ = dolfinx.Function(Q)
dolfiny.interpolation.interpolate(u_, u__)
dolfiny.interpolation.interpolate(w_, w__)
dolfiny.interpolation.interpolate(r_, r__)
u__ = u__.vector[x0_idx]
w__ = w__.vector[x0_idx]
r__ = r__.vector[x0_idx]

# Plot
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
fig, ax1 = plt.subplots()
ax1.plot(x0[:, 0], x0[:, 2], '.-', color='tab:gray', label='undeformed')
ax1.plot(x0[-1, 0], x0[-1, 2], 'o', color='tab:gray')  # undeformed: mark end
ax1.plot(x0[:, 0] + u__, x0[:, 2] + w__, '.-', color='tab:red', label='deformed')
ax1.plot(x0[-1, 0] + u__[-1], x0[-1, 2] + w__[-1], 'o', color='tab:blue')  # deformed: mark end
ax1.set_xlabel(r'coordinate $x$, displacement $[m]$', fontsize=12)
ax1.set_ylabel(r'coordinate $z$, displacement $[m]$', fontsize=12)
# ax1.plot(x0[:, 0], u__, '-', color='tab:green', label='$u(x)$')
# ax1.plot(x0[:, 0], w__, '-', color='tab:blue', label='$w(x)$')
ax1.grid(linewidth=0.5)
ax1.set_title(r'geometrically exact beam (displacement-based)', fontsize=12)
ax1.legend(loc='upper left')
ax1.invert_yaxis()
ax1.axis('equal')
# ax2 = ax1.twinx()
# ax2.plot(x0[:, 0], r__/(2*np.pi), '.', color='tab:brown', label='$r(x)$')
# ax2.yaxis.tick_right()
# ax2.yaxis.set_label_position("right")
# ax2.set_ylabel(r"normalised rotation $\psi/2\pi$ $[-]$", fontsize=12)
# ax2.invert_yaxis()
fig.tight_layout()
fig.savefig(name + '_result.pdf')
