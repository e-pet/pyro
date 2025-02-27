# Copyright (c) 2017-2019 Uber Technologies, Inc.
# SPDX-License-Identifier: Apache-2.0

import os
from tempfile import TemporaryDirectory
from unittest import TestCase

import pytest
import torch
from torch.distributions import constraints

import pyro
import pyro.distributions as dist
import pyro.optim as optim
from pyro import poutine
from pyro.distributions import Normal, Uniform
from pyro.infer import SVI, Trace_ELBO, TraceGraph_ELBO
from pyro.nn.module import PyroModule, PyroParam, to_pyro_module_
from pyro.optim.optim import is_scheduler
from tests.common import assert_equal


class OptimTests(TestCase):
    def setUp(self):
        # normal-normal; known covariance
        self.lam0 = torch.tensor([0.1])  # precision of prior
        self.loc0 = torch.tensor([0.5])  # prior mean
        # known precision of observation noise
        self.lam = torch.tensor([6.0])
        self.data = torch.tensor([1.0])  # a single observation

    def test_per_param_optim(self):
        self.do_test_per_param_optim("loc_q", "log_sig_q")
        self.do_test_per_param_optim("log_sig_q", "loc_q")

    # make sure lr=0 gets propagated correctly to parameters of our choice
    def do_test_per_param_optim(self, fixed_param, free_param):
        pyro.clear_param_store()

        def model():
            prior_dist = Normal(self.loc0, torch.pow(self.lam0, -0.5))
            loc_latent = pyro.sample("loc_latent", prior_dist)
            x_dist = Normal(loc_latent, torch.pow(self.lam, -0.5))
            pyro.sample("obs", x_dist, obs=self.data)
            return loc_latent

        def guide():
            loc_q = pyro.param("loc_q", torch.zeros(1, requires_grad=True))
            log_sig_q = pyro.param("log_sig_q", torch.zeros(1, requires_grad=True))
            sig_q = torch.exp(log_sig_q)
            pyro.sample("loc_latent", Normal(loc_q, sig_q))

        def optim_params(param_name):
            if param_name == fixed_param:
                return {"lr": 0.00}
            elif param_name == free_param:
                return {"lr": 0.01}

        adam = optim.Adam(optim_params)
        adam2 = optim.Adam(optim_params)
        svi = SVI(model, guide, adam, loss=TraceGraph_ELBO())
        svi2 = SVI(model, guide, adam2, loss=TraceGraph_ELBO())

        svi.step()
        adam_initial_step_count = list(adam.get_state()["loc_q"]["state"].items())[0][
            1
        ]["step"]
        with TemporaryDirectory() as tempdir:
            filename = os.path.join(tempdir, "optimizer_state.pt")
            adam.save(filename)
            svi.step()
            adam_final_step_count = list(adam.get_state()["loc_q"]["state"].items())[0][
                1
            ]["step"]
            adam2.load(filename)
        svi2.step()
        adam2_step_count_after_load_and_step = list(
            adam2.get_state()["loc_q"]["state"].items()
        )[0][1]["step"]

        assert adam_initial_step_count == 1
        assert adam_final_step_count == 2
        assert adam2_step_count_after_load_and_step == 2

        free_param_unchanged = torch.equal(pyro.param(free_param).data, torch.zeros(1))
        fixed_param_unchanged = torch.equal(
            pyro.param(fixed_param).data, torch.zeros(1)
        )
        assert fixed_param_unchanged and not free_param_unchanged


@pytest.mark.parametrize(
    "scheduler",
    [
        optim.LambdaLR(
            {
                "optimizer": torch.optim.SGD,
                "optim_args": {"lr": 0.01},
                "lr_lambda": lambda epoch: 2.0**epoch,
            }
        ),
        optim.StepLR(
            {
                "optimizer": torch.optim.SGD,
                "optim_args": {"lr": 0.01},
                "gamma": 2,
                "step_size": 1,
            }
        ),
        optim.ExponentialLR(
            {"optimizer": torch.optim.SGD, "optim_args": {"lr": 0.01}, "gamma": 2}
        ),
        optim.ReduceLROnPlateau(
            {
                "optimizer": torch.optim.SGD,
                "optim_args": {"lr": 1.0},
                "factor": 0.1,
                "patience": 1,
            }
        ),
    ],
)
def test_dynamic_lr(scheduler):
    pyro.clear_param_store()

    def model():
        sample = pyro.sample("latent", Normal(torch.tensor(0.0), torch.tensor(0.3)))
        return pyro.sample(
            "obs", Normal(sample, torch.tensor(0.2)), obs=torch.tensor(0.1)
        )

    def guide():
        loc = pyro.param("loc", torch.tensor(0.0))
        scale = pyro.param("scale", torch.tensor(0.5), constraint=constraints.positive)
        pyro.sample("latent", Normal(loc, scale))

    svi = SVI(model, guide, scheduler, loss=TraceGraph_ELBO())
    for epoch in range(4):
        svi.step()
        svi.step()
        loc = pyro.param("loc").unconstrained()
        opt_loc = scheduler.optim_objs[loc].optimizer
        opt_scale = scheduler.optim_objs[loc].optimizer
        if issubclass(
            scheduler.pt_scheduler_constructor,
            torch.optim.lr_scheduler.ReduceLROnPlateau,
        ):
            scheduler.step(1.0)
            if epoch == 2:
                assert opt_loc.state_dict()["param_groups"][0]["lr"] == 0.1
                assert opt_scale.state_dict()["param_groups"][0]["lr"] == 0.1
            if epoch == 4:
                assert opt_loc.state_dict()["param_groups"][0]["lr"] == 0.01
                assert opt_scale.state_dict()["param_groups"][0]["lr"] == 0.01
            continue
        assert opt_loc.state_dict()["param_groups"][0]["initial_lr"] == 0.01
        assert opt_scale.state_dict()["param_groups"][0]["initial_lr"] == 0.01
        if epoch == 0:
            scheduler.step()
            assert opt_loc.state_dict()["param_groups"][0]["lr"] == 0.02
            assert opt_scale.state_dict()["param_groups"][0]["lr"] == 0.02
            assert abs(pyro.param("loc").item()) > 1e-5
            assert abs(pyro.param("scale").item() - 0.5) > 1e-5
        if epoch == 2:
            scheduler.step()
            assert opt_loc.state_dict()["param_groups"][0]["lr"] == 0.04
            assert opt_scale.state_dict()["param_groups"][0]["lr"] == 0.04


@pytest.mark.parametrize(
    "factory", [optim.Adam, optim.ClippedAdam, optim.DCTAdam, optim.RMSprop, optim.SGD]
)
def test_autowrap(factory):
    instance = factory({})
    assert instance.pt_optim_constructor.__name__ == factory.__name__


@pytest.mark.parametrize("pyro_optim", [optim.Adam, optim.SGD])
@pytest.mark.parametrize("clip", ["clip_norm", "clip_value"])
@pytest.mark.parametrize("value", [1.0, 3.0, 5.0])
def test_clip_norm(pyro_optim, clip, value):
    x1 = torch.tensor(0.0, requires_grad=True)
    x2 = torch.tensor(0.0, requires_grad=True)
    opt_c = pyro_optim({"lr": 1.0}, {clip: value})
    opt = pyro_optim({"lr": 1.0})
    for step in range(3):
        x1.backward(Uniform(value, value + 3.0).sample())
        x2.backward(torch.tensor(value))
        opt_c([x1])
        opt([x2])
        assert_equal(x1.grad, torch.tensor(value))
        assert_equal(x2.grad, torch.tensor(value))
        assert_equal(x1, x2)
        opt_c.optim_objs[x1].zero_grad()
        opt.optim_objs[x2].zero_grad()


@pytest.mark.parametrize("clip_norm", [1.0, 3.0, 5.0])
def test_clippedadam_clip(clip_norm):
    x1 = torch.tensor(0.0, requires_grad=True)
    x2 = torch.tensor(0.0, requires_grad=True)
    opt_ca = optim.clipped_adam.ClippedAdam(
        params=[x1], lr=1.0, lrd=1.0, clip_norm=clip_norm
    )
    opt_a = torch.optim.Adam(params=[x2], lr=1.0)
    for step in range(3):
        opt_ca.zero_grad()
        opt_a.zero_grad()
        x1.backward(Uniform(clip_norm, clip_norm + 3.0).sample())
        x2.backward(torch.tensor(clip_norm))
        opt_ca.step()
        opt_a.step()
        assert_equal(x1, x2)


@pytest.mark.parametrize("clip_norm", [1.0, 3.0, 5.0])
def test_clippedadam_pass(clip_norm):
    x1 = torch.tensor(0.0, requires_grad=True)
    x2 = torch.tensor(0.0, requires_grad=True)
    opt_ca = optim.clipped_adam.ClippedAdam(
        params=[x1], lr=1.0, lrd=1.0, clip_norm=clip_norm
    )
    opt_a = torch.optim.Adam(params=[x2], lr=1.0)
    for step in range(3):
        g = Uniform(-clip_norm, clip_norm).sample()
        opt_ca.zero_grad()
        opt_a.zero_grad()
        x1.backward(g)
        x2.backward(g)
        opt_ca.step()
        opt_a.step()
        assert_equal(x1, x2)


@pytest.mark.parametrize("lrd", [1.0, 3.0, 5.0])
def test_clippedadam_lrd(lrd):
    x1 = torch.tensor(0.0, requires_grad=True)
    orig_lr = 1.0
    opt_ca = optim.clipped_adam.ClippedAdam(params=[x1], lr=orig_lr, lrd=lrd)
    for step in range(3):
        g = Uniform(-5.0, 5.0).sample()
        x1.backward(g)
        opt_ca.step()
        assert opt_ca.param_groups[0]["lr"] == orig_lr * lrd ** (step + 1)


def test_dctadam_param_subsample():
    outer_size = 7
    middle_size = 2
    inner_size = 11
    outer_subsize = 3
    inner_subsize = 4
    event_size = 5

    def model():
        with pyro.plate("outer", outer_size, subsample_size=outer_subsize, dim=-3):
            with pyro.plate("inner", inner_size, subsample_size=inner_subsize, dim=-1):
                pyro.param(
                    "loc",
                    torch.randn(outer_size, middle_size, inner_size, event_size),
                    event_dim=1,
                )

    optimizer = optim.DCTAdam({"lr": 1.0, "subsample_aware": True})
    model()
    param = pyro.param("loc").unconstrained()
    param.sum().backward()
    pre_optimized_value = param.detach().clone()
    optimizer({param})
    expected_num_changes = outer_subsize * middle_size * inner_subsize * event_size
    actual_num_changes = ((param - pre_optimized_value) != 0).sum().item()
    assert actual_num_changes == expected_num_changes

    for i in range(1000):
        pyro.infer.util.zero_grads({param})
        model()  # generate new subsample indices
        param.pow(2).sum().backward()
        optimizer({param})

    assert_equal(param, param.new_zeros(param.shape), prec=1e-2)


def test_name_preserved_by_to_pyro_module():
    features = torch.randn(4)
    data = torch.randn(3)

    class Model(PyroModule):
        def __init__(self):
            super().__init__()
            self.scale = PyroParam(torch.ones(3), constraints.positive)
            self.loc = torch.nn.Linear(4, 3)

        def forward(self, features, data):
            loc = self.loc(features)
            scale = self.scale
            with pyro.plate("data", len(data)):
                pyro.sample("obs", dist.Normal(loc, scale), obs=data)

    model = Model()
    params = list(model.parameters())
    param_names = set()

    def optim_config(param_name):
        param_names.add(param_name)
        return {"lr": 0.0}

    # Record while model.loc is an nn.Module.
    loss = poutine.trace(model).get_trace(features, data).log_prob_sum()
    loss.backward()
    adam = optim.Adam(optim_config)
    adam(params)
    assert param_names
    expected_param_names = param_names.copy()
    del adam, loss
    param_names.clear()
    pyro.clear_param_store()

    # Record while model.loc is a PyroModule.
    to_pyro_module_(model.loc)
    loss = poutine.trace(model).get_trace(features, data).log_prob_sum()
    loss.backward()
    adam = optim.Adam(optim_config)
    adam(params)
    assert param_names
    actual_param_names = param_names.copy()
    del adam, loss
    param_names.clear()
    pyro.clear_param_store()

    assert actual_param_names == {"scale", "loc.weight", "loc.bias"}
    assert actual_param_names == expected_param_names


@pytest.mark.parametrize(
    "Optim, config",
    [
        (optim.Adam, {"lr": 0.01}),
        (optim.ClippedAdam, {"lr": 0.01}),
        (optim.DCTAdam, {"lr": 0.01}),
        (optim.RMSprop, {"lr": 0.01}),
        (optim.SGD, {"lr": 0.01}),
        (
            optim.LambdaLR,
            {
                "optimizer": torch.optim.SGD,
                "optim_args": {"lr": 0.01},
                "lr_lambda": lambda epoch: 0.9**epoch,
            },
        ),
        (
            optim.StepLR,
            {
                "optimizer": torch.optim.SGD,
                "optim_args": {"lr": 0.01},
                "gamma": 0.9,
                "step_size": 1,
            },
        ),
        (
            optim.ExponentialLR,
            {"optimizer": torch.optim.SGD, "optim_args": {"lr": 0.01}, "gamma": 0.9},
        ),
        (
            optim.ReduceLROnPlateau,
            {
                "optimizer": torch.optim.SGD,
                "optim_args": {"lr": 1.0},
                "factor": 0.1,
                "patience": 1,
            },
        ),
    ],
)
def test_checkpoint(Optim, config):
    def model():
        x_scale = pyro.param(
            "x_scale", torch.tensor(1.0), constraint=constraints.positive
        )
        z = pyro.sample("z", Normal(0, 1))
        return pyro.sample("x", Normal(z, x_scale), obs=torch.tensor(0.1))

    def guide():
        z_loc = pyro.param("z_loc", torch.tensor(0.0))
        z_scale = pyro.param(
            "z_scale", torch.tensor(0.5), constraint=constraints.positive
        )
        pyro.sample("z", Normal(z_loc, z_scale))

    store = pyro.get_param_store()

    def step(svi, optimizer):
        svi.step()
        if is_scheduler(optimizer):
            if issubclass(
                optimizer.pt_scheduler_constructor,
                torch.optim.lr_scheduler.ReduceLROnPlateau,
            ):
                optimizer.step(1.0)
            else:
                optimizer.step()
        snapshot = {k: v.data.clone() for k, v in store.items()}
        return snapshot

    # Try without a checkpoint.
    expected = []
    store.clear()
    pyro.set_rng_seed(20210811)
    optimizer = Optim(config.copy())
    svi = SVI(model, guide, optimizer, Trace_ELBO())
    for _ in range(5 + 10):
        expected.append(step(svi, optimizer))
    del svi, optimizer

    # Try with a checkpoint.
    actual = []
    store.clear()
    pyro.set_rng_seed(20210811)
    optimizer = Optim(config.copy())
    svi = SVI(model, guide, optimizer, Trace_ELBO())
    for _ in range(5):
        actual.append(step(svi, optimizer))
    # checkpoint
    with TemporaryDirectory() as tempdir:
        optim_filename = os.path.join(tempdir, "optimizer_state.pt")
        param_filename = os.path.join(tempdir, "param_store.pt")

        optimizer.save(optim_filename)
        store.save(param_filename)
        del optimizer, svi
        store.clear()

        store.load(param_filename)
        optimizer = Optim(config.copy())
        optimizer.load(optim_filename)
    svi = SVI(model, guide, optimizer, Trace_ELBO())
    for _ in range(10):
        actual.append(step(svi, optimizer))

    assert_equal(actual, expected)
