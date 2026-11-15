"""Estimator wrappers used by train.py that also need to unpickle correctly
inside play.py / play2.py / record.py.

A class defined directly in train.py pickles with __module__ == "__main__",
because that is how it is always run (`python train.py ...`). Loading that
same pickle from any OTHER script's __main__ - which is exactly what
agent.py's Agent does when it loads models/policy.pkl - fails, because that
script's __main__ has no such class. Anything shipped inside a policy has to
live in the ajbot package instead, where its module name is the same no
matter who imports it.
"""
import os

import numpy as np


class DenseLabelXGB:
    """Adapts an XGBoost classifier to the sparse label spaces the other
    models in train.py already tolerate for free.

    XGBoost's fit() demands y's unique values be a contiguous 0..k-1 range.
    sklearn's classifiers don't care, which matters because a train/holdout
    SPLIT routinely leaves out some rare action (a defensive label, say)
    that only shows up in the held-out match - not missing from the corpus,
    just from this half of it. Fits on a dense local re-encoding and expands
    predict_proba back to the full action space, since agent.py indexes its
    columns positionally against the full `actions` list.
    """

    def __init__(self, estimator, n_actions):
        self.estimator = estimator
        self.n_actions = n_actions

    def fit(self, X, y, sample_weight=None):
        self.classes_, y_dense = np.unique(y, return_inverse=True)
        if sample_weight is None:
            self.estimator.fit(X, y_dense)
        else:
            self.estimator.fit(X, y_dense, sample_weight=sample_weight)
        return self

    def predict(self, X):
        return self.classes_[self.estimator.predict(X)]

    def predict_proba(self, X):
        p = np.zeros((len(X), self.n_actions), dtype=np.float64)
        p[:, self.classes_] = self.estimator.predict_proba(X)
        return p

    def score(self, X, y):
        return float(np.mean(self.predict(X) == y))

    def get_params(self, deep=True):
        return self.estimator.get_params(deep=deep)

    def set_params(self, **kw):
        self.estimator.set_params(**kw)
        return self


class TorchMLP:
    """A wider, regularised neural action head, trained with PyTorch on the
    GPU and run in plain numpy.

    sklearn's MLP is CPU-only, small and unweighted-by-default; this one
    trains on the full corpus in seconds with sample weights, dropout, label
    smoothing and early stopping on a held-back slice. The weights are stored
    as numpy arrays, so a shipped policy needs neither torch nor a GPU in live
    play - one forward pass for one tick is a few small matrix products.
    """

    def __init__(self, n_actions, hidden=(512, 256), dropout=0.2, epochs=40,
                 lr=2e-3, weight_decay=1e-4, smoothing=0.05, batch=2048, seed=0):
        self.n_actions = n_actions
        self.hidden = hidden
        self.dropout = dropout
        self.epochs = epochs
        self.lr = lr
        self.weight_decay = weight_decay
        self.smoothing = smoothing
        self.batch = batch
        self.seed = seed

    def fit(self, X, y, sample_weight=None):
        import torch
        from torch import nn
        torch.manual_seed(self.seed)
        dev = "cuda" if torch.cuda.is_available() and not os.environ.get("AJBOT_NO_GPU") else "cpu"
        X = np.asarray(X, np.float32)
        self.n_features_in_ = X.shape[1]
        self.mu_ = X.mean(0)
        self.sd_ = X.std(0) + 1e-6
        Z = (X - self.mu_) / self.sd_
        w = np.ones(len(X), np.float32) if sample_weight is None else np.asarray(sample_weight, np.float32)
        rng = np.random.default_rng(self.seed)
        idx = rng.permutation(len(X))
        n_val = max(1, len(X) // 20)
        va, tr = idx[:n_val], idx[n_val:]
        t = lambda a, dt=torch.float32: torch.as_tensor(a, dtype=dt, device=dev)
        Ztr, ytr, wtr = t(Z[tr]), t(y[tr], torch.long), t(w[tr])
        Zva, yva = t(Z[va]), t(y[va], torch.long)
        layers, d = [], X.shape[1]
        for h in self.hidden:
            layers += [nn.Linear(d, h), nn.GELU(approximate="tanh"), nn.Dropout(self.dropout)]
            d = h
        layers.append(nn.Linear(d, self.n_actions))
        net = nn.Sequential(*layers).to(dev)
        opt = torch.optim.AdamW(net.parameters(), lr=self.lr, weight_decay=self.weight_decay)
        sched = torch.optim.lr_scheduler.CosineAnnealingLR(opt, self.epochs)
        loss_fn = nn.CrossEntropyLoss(reduction="none", label_smoothing=self.smoothing)
        best, best_state, stale = -1.0, None, 0
        for _ in range(self.epochs):
            net.train()
            perm = torch.randperm(len(Ztr), device=dev)
            for i in range(0, len(perm), self.batch):
                b = perm[i:i + self.batch]
                loss = (loss_fn(net(Ztr[b]), ytr[b]) * wtr[b]).sum() / wtr[b].sum()
                opt.zero_grad()
                loss.backward()
                opt.step()
            sched.step()
            net.eval()
            with torch.no_grad():
                acc = float((net(Zva).argmax(1) == yva).float().mean())
            if acc > best:
                best, stale = acc, 0
                best_state = {k: v.detach().cpu().numpy().copy() for k, v in net.state_dict().items()}
            else:
                stale += 1
                if stale >= 6:
                    break
        lin = [k[:-len(".weight")] for k in best_state if k.endswith(".weight")]
        self.layers_ = [(best_state[f"{k}.weight"].T.copy(), best_state[f"{k}.bias"].copy()) for k in lin]
        self.val_acc_ = best
        return self

    def predict_proba(self, X):
        h = (np.asarray(X, np.float32) - self.mu_) / self.sd_
        for i, (W, b) in enumerate(self.layers_):
            h = h @ W + b
            if i < len(self.layers_) - 1:
                h = 0.5 * h * (1.0 + np.tanh(0.7978845608 * (h + 0.044715 * h ** 3)))   # GELU
        h = h - h.max(1, keepdims=True)
        e = np.exp(h)
        return (e / e.sum(1, keepdims=True)).astype(np.float64)

    def predict(self, X):
        return self.predict_proba(X).argmax(1)

    def score(self, X, y):
        return float(np.mean(self.predict(X) == y))


class FastForest:
    """Single-row predict for a fitted sklearn forest regressor, without
    joblib. Policies are trained with n_jobs=-1, and live play then paid for
    a thread-pool dispatch over every core for each one-row prediction
    (~9 ms per call on the 32-thread box, 2026-11-10). Sums the same trees
    in the same order as sklearn, so the output is identical."""

    def __init__(self, forest):
        self.forest = forest
        self.trees = [e.tree_ for e in forest.estimators_]
        self.n_outputs = forest.n_outputs_

    def predict(self, X):
        X = np.ascontiguousarray(X, dtype=np.float32)
        out = np.zeros((X.shape[0], self.n_outputs), dtype=np.float64)
        for t in self.trees:
            out += t.predict(X)[:, :, 0]
        out /= len(self.trees)
        return out if self.n_outputs > 1 else out[:, 0]

    def __getattr__(self, name):
        return getattr(self.forest, name)


def tune_for_live(model, threads=1):
    """Make a trained model cheap to call one row at a time: forests get the
    joblib-free path above; XGBoost gets a small thread count instead of
    every core. Measured on the box per one-row call (2026-11-10): movement
    forest 15.5 -> 0.2 ms, the five critic heads 2.9 -> 0.7 ms on 1 thread,
    the 27-class action head 2.7 -> 1.2 ms on 4 threads (1 thread: 4.8 ms).
    Predictions do not change (the forest only by float summation order)."""
    if isinstance(model, dict):
        return {k: tune_for_live(v) for k, v in model.items()}
    if isinstance(model, DenseLabelXGB):
        model.estimator = tune_for_live(model.estimator, threads=4)
        return model
    if hasattr(model, "estimators_") and hasattr(model, "n_outputs_") \
            and not hasattr(model, "classes_"):
        return FastForest(model)
    if type(model).__module__.startswith("xgboost"):
        try:
            model.set_params(n_jobs=threads)
        except Exception:
            pass
    return model
