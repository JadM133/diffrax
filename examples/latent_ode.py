###########
#
# This example trains a Latent ODE (https://arxiv.org/abs/1907.03907).
# In this case, it's on a simple dataset of decaying oscillators.
# That is, 2-dimensional time series that look like:
#
#
#
# xx    ***
#     **   *
#   x*      **
#   *x
#     x       *
#  *           *                  xxxxx
# *    x        *               xx    xx *******
#                              x        x       **
#       x        *            x        * x        *                  xxxxxxxx  ******
#        x        *          x        *   x        *              xxx       *xx      *
#                           x        *     xx       **           x        **   xx
#         x        *       x        *        x        *        xx       **       xx
#                   *     x        *          x        **     x        *           xxx
#          x         *            *            x         *  xx       **
#           x         *  x       *              xx        xx*     ***
#            x         *x       *                 xxx  xxx   *****
#             x        x*      *                     xx
#              x     xx  ******
#               xxxxx
#
#
#
# The model is trained to generate samples that look like this.
#
###########
#
# What's really nice about this example is that we will take the underlying data to be
# irregularly sampled. We will have different observation times for different batch
# elements.
#
# Most differential equation libraries will struggle with this, as they usually mandate
# that the differential equation be solved over the same timespan for all batch
# elements. Working around this can involve programming complexity like outputting at
# lots and lots of times (the union of all the observations times in the batch), or
# mathematical complexities like reparameterising the differentiating equation.
#
# However Diffrax is capable of handling this without such issues! You can `vmap` over
# different integration times for different batch elements.
#
###########

import functools as ft
import pathlib
import time

import diffrax
import equinox as eqx
import fire
import jax
import jax.nn as jnn
import jax.numpy as jnp
import jax.random as jrandom
import matplotlib
import matplotlib.pyplot as plt
import numpy as np
import optax


matplotlib.rcParams.update({"font.size": 30})


here = pathlib.Path(__file__).resolve().parent


###########
# The vector field.
# Note its overall structure of
# scalar * tanh(mlp(y))
# which is a good structure for Latent ODEs.
###########
class Func(eqx.Module):
    scale: jnp.ndarray
    mlp: eqx.nn.MLP

    @ft.partial(eqx.filter_jit, filter_spec=eqx.is_array)
    def __call__(self, t, y, args):
        return self.scale * self.mlp(y)


###########
# Wrap up the differential equation solve into a model.
###########
class LatentODE(eqx.Module):
    solver: diffrax.AbstractSolver
    rnn_cell: eqx.nn.GRUCell

    hidden_to_latent: eqx.nn.Linear
    latent_to_hidden: eqx.nn.MLP
    hidden_to_data: eqx.nn.Linear

    hidden_size: int
    latent_size: int

    def __init__(
        self, *, data_size, hidden_size, latent_size, width_size, depth, key, **kwargs
    ):
        super().__init__(**kwargs)

        mkey, gkey, hlkey, lhkey, hdkey = jrandom.split(key, 5)

        scale = jnp.ones(())
        mlp = eqx.nn.MLP(
            in_size=hidden_size,
            out_size=hidden_size,
            width_size=width_size,
            depth=depth,
            activation=jnn.softplus,
            final_activation=jnn.tanh,
            key=mkey,
        )
        self.solver = diffrax.dopri5(Func(scale, mlp))
        self.rnn_cell = eqx.nn.GRUCell(data_size + 1, hidden_size, key=gkey)

        self.hidden_to_latent = eqx.nn.Linear(hidden_size, 2 * latent_size, key=hlkey)
        self.latent_to_hidden = eqx.nn.MLP(
            latent_size, hidden_size, width_size=width_size, depth=depth, key=lhkey
        )
        self.hidden_to_data = eqx.nn.Linear(hidden_size, data_size, key=hdkey)

        self.hidden_size = hidden_size
        self.latent_size = latent_size

    ###########
    # Encoder of the VAE
    ###########
    @ft.partial(eqx.filter_jit, filter_spec=eqx.is_array)
    def _latent(self, ts, ys, key):
        data = jnp.concatenate([ts[:, None], ys], axis=1)
        hidden = jnp.zeros((self.hidden_size,))
        for data_i in reversed(data):
            hidden = self.rnn_cell(data_i, hidden)
        context = self.hidden_to_latent(hidden)
        mean, logstd = context[: self.latent_size], context[self.latent_size :]
        std = jnp.exp(logstd)
        latent = mean + jrandom.normal(key, (self.latent_size,)) * std
        return latent, mean, std

    ###########
    # Decoder of the VAE
    ###########
    def _sample(self, ts, latent):
        y0 = self.latent_to_hidden(latent)
        sol = diffrax.diffeqsolve(
            self.solver,
            ts[0],
            ts[-1],
            y0,
            dt0=0.4,  # selected as a reasonable choice for this problem
            saveat=diffrax.SaveAt(ts=ts),
        )
        return jax.vmap(self.hidden_to_data)(sol.ys)

    @staticmethod
    @jax.jit
    def _loss(ys, pred_ys, mean, std):
        # -log p_θ with Gaussian p_θ
        reconstruction_loss = 0.5 * jnp.sum((ys - pred_ys) ** 2)
        # KL(N(mean, std^2) || N(0, 1))
        variational_loss = 0.5 * jnp.sum(mean ** 2 + std ** 2 - 2 * jnp.log(std) - 1)
        return reconstruction_loss + variational_loss

    ###########
    # Run both encoder and decoder during training.
    ###########
    def train(self, ts, ys, *, key):
        latent, mean, std = self._latent(ts, ys, key)
        pred_ys = self._sample(ts, latent)
        return self._loss(ys, pred_ys, mean, std)

    ###########
    # Run just the decoder during inference.
    ###########
    def sample(self, ts, *, key):
        latent = jrandom.normal(key, (self.latent_size,))
        return self._sample(ts, latent)


###########
# Toy dataset of decaying oscillators.
#
# By way of illustration we set this up as a differential equaiton and solve this using
# Diffrax as well. (Despite this being an autonomous linear ODE, for which a
# closed-form solution is actually available.)
###########
def get_data(dataset_size, *, key):
    ykey, tkey1, tkey2 = jrandom.split(key, 3)

    y0 = jrandom.normal(ykey, (dataset_size, 2))

    t0 = 0
    t1 = 2 + jrandom.uniform(tkey1, (dataset_size,))
    ts = jrandom.uniform(tkey2, (dataset_size, 20)) * (t1[:, None] - t0) + t0
    ts = jnp.sort(ts)

    def func(t, y, args):
        return jnp.array([[-0.1, 1.3], [-1, -0.1]]) @ y

    def solve(ts, y0):
        sol = diffrax.diffeqsolve(
            diffrax.tsit5(func),
            ts[0],
            ts[-1],
            y0,
            dt0=0.1,
            saveat=diffrax.SaveAt(ts=ts),
        )
        return sol.ys

    ys = jax.vmap(solve)(ts, y0)

    return ts, ys


def dataloader(arrays, batch_size, *, key):
    dataset_size = arrays[0].shape[0]
    assert all(array.shape[0] == dataset_size for array in arrays)
    indices = jnp.arange(dataset_size)
    while True:
        perm = jrandom.permutation(key, indices)
        (key,) = jrandom.split(key, 1)
        start = 0
        end = batch_size
        while start < dataset_size:
            batch_perm = perm[start:end]
            yield tuple(array[batch_perm] for array in arrays)
            start = end
            end = start + batch_size


def main(
    out_path=here / "latent_ode.png",
    dataset_size=10000,
    batch_size=256,
    lr=1e-2,
    steps=250,
    save_every=50,
    hidden_size=16,
    latent_size=16,
    width_size=16,
    depth=2,
    seed=5678,
):
    out_path = str(out_path)
    key = jrandom.PRNGKey(seed)
    data_key, model_key, loader_key, train_key, sample_key = jrandom.split(key, 5)

    ts, ys = get_data(dataset_size, key=data_key)

    model = LatentODE(
        data_size=ys.shape[-1],
        hidden_size=hidden_size,
        latent_size=latent_size,
        width_size=width_size,
        depth=depth,
        key=model_key,
    )

    @eqx.filter_value_and_grad
    def loss(model, ts_i, ys_i, *, key_i):
        batch_size, _ = ts_i.shape
        key_i = jrandom.split(key_i, batch_size)
        loss = jax.vmap(model.train)(ts_i, ys_i, key=key_i)
        return jnp.mean(loss)

    optim = optax.adam(lr)
    opt_state = optim.init(
        jax.tree_map(lambda leaf: leaf if eqx.is_inexact_array(leaf) else None, model)
    )

    # Plot results
    num_plots = 1 + (steps - 1) // save_every
    if ((steps - 1) % save_every) != 0:
        num_plots += 1
    fig, axs = plt.subplots(1, num_plots, figsize=(num_plots * 8, 8))
    axs[0].set_ylabel("x")
    axs = iter(axs)
    for step, (ts_i, ys_i) in zip(
        range(steps), dataloader((ts, ys), batch_size, key=loader_key)
    ):
        start = time.time()
        value, grads = loss(model, ts_i, ys_i, key_i=train_key)
        end = time.time()

        (train_key,) = jrandom.split(train_key, 1)
        updates, opt_state = optim.update(grads, opt_state)
        model = eqx.apply_updates(model, updates)
        print(f"Step: {step}, Loss: {value}, Computation time: {end - start}")

        if (step % save_every) == 0 or step == steps - 1:
            ax = next(axs)
            # Sample over a longer time interval than we trained on. The model will be
            # sufficiently good that it will correctly extrapolate!
            sample_t = jnp.linspace(0, 12, 300)
            sample_y = model.sample(sample_t, key=sample_key)
            sample_t = np.asarray(sample_t)
            sample_y = np.asarray(sample_y)
            ax.plot(sample_t, sample_y[:, 0])
            ax.plot(sample_t, sample_y[:, 1])
            ax.set_xticks([])
            ax.set_yticks([])
            ax.set_xlabel("t")

    plt.savefig(out_path)
    plt.close()


if __name__ == "__main__":
    fire.Fire(main)