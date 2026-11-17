"""Behaviour-cloning baselines for both heads.

    python training/train.py                  # both heads, newest run
    python training/train.py --runs "runs/*.jsonl"
    python training/train.py --mode frames    # action head on every tick (see warning)

Two separate problems:
  action   - classification over ECommandIds, judged against always guessing
             the most common action.
  movement - regression onto [sin(lang), cos(lang), lscale], judged against
             always predicting the training mean.

Splits are chronological. A random split leaks, because neighbouring ticks are
nearly the same moment.
"""
import os, sys  # noqa: E401
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))   # for _root, also under python -P
import _root  # noqa: F401,E402  (repo root + script folders on sys.path)
import argparse
import collections
import glob
import os
from concurrent.futures import ProcessPoolExecutor
from functools import partial
from pathlib import Path

import numpy as np

from ajbot import dataset as D
from ajbot import scrub, tickcache
from ajbot.parallel import safe_worker_count

WORKERS = min(32, os.cpu_count() or 1)

_GPU = None


def gpu_available():
    """True once, if xgboost can actually fit on the CUDA device - not just
    import. Training here is small enough that a broken GPU stack should
    fall back to CPU rather than fail the whole run."""
    global _GPU
    if _GPU is None:
        try:
            if os.environ.get("AJBOT_NO_GPU"):
                raise RuntimeError("AJBOT_NO_GPU is set")
            import warnings
            import xgboost as xgb
            # With no CUDA device xgboost does not fail - it warns and quietly
            # trains on the CPU - so the warning is what says there is no GPU.
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                xgb.XGBClassifier(n_estimators=1, device="cuda",
                                  tree_method="hist").fit([[0, 1], [1, 0]], [0, 1])
            if any("GPU" in str(w.message) for w in caught):
                raise RuntimeError("no CUDA device visible")
            _GPU = True
        except Exception as e:
            print(f"  (GPU training unavailable: {e}; falling back to CPU models)")
            _GPU = False
    return _GPU


def fit_weighted(model, X, y, w):
    """fit() with per-sample reward weights, whatever the estimator.

    Pipelines route the weight to their final step. Estimators that take no
    sample_weight (older scikit-learn MLPs) get a weighted resample instead,
    which has the same expected effect.
    """
    if w is None:
        return model.fit(X, y)
    if hasattr(model, "steps"):
        name = model.steps[-1][0]
        try:
            return model.fit(X, y, **{f"{name}__sample_weight": w})
        except TypeError:
            pass
    else:
        try:
            return model.fit(X, y, sample_weight=w)
        except TypeError:
            pass
    rng = np.random.default_rng(0)
    p = w / w.sum()
    idx = rng.choice(len(X), size=len(X), replace=True, p=p)
    return model.fit(X[idx], y[idx])


def action_models(n_actions=None):
    """Candidate action heads. The one that wins on the held-out match is the
    one save_policy ships - it used to ship the MLP unconditionally, which
    overfits (99.7% train, 25.7% holdout) while the other two held 38%.

    `n_actions` (the full action vocabulary size) is only needed to size the
    GPU model's predict_proba output; the CPU models size themselves from
    whatever labels they actually see.
    """
    from sklearn.ensemble import RandomForestClassifier
    from sklearn.linear_model import LogisticRegression
    from sklearn.neural_network import MLPClassifier
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler
    m = {
        "logistic regression": make_pipeline(StandardScaler(),
                                             LogisticRegression(max_iter=3000, C=0.5)),
        "random forest": RandomForestClassifier(n_estimators=250,
                                                min_samples_leaf=3,
                                                random_state=0, n_jobs=-1),
        "mlp (128,64)": make_pipeline(StandardScaler(),
                                      MLPClassifier(hidden_layer_sizes=(128, 64),
                                                    alpha=1e-3, max_iter=600,
                                                    early_stopping=True,
                                                    random_state=0)),
    }
    if n_actions:
        import xgboost as xgb
        from ajbot.model_utils import DenseLabelXGB
        if gpu_available():
            clf = xgb.XGBClassifier(n_estimators=300, max_depth=6, learning_rate=0.1,
                                    device="cuda", tree_method="hist", random_state=0)
            m["xgboost (gpu)"] = DenseLabelXGB(clf, n_actions)
        else:
            # Same model on the CPU (e.g. the laptop, which has no CUDA GPU).
            clf = xgb.XGBClassifier(n_estimators=300, max_depth=6, learning_rate=0.1,
                                    device="cpu", tree_method="hist", n_jobs=-1, random_state=0)
            m["xgboost (cpu)"] = DenseLabelXGB(clf, n_actions)
        try:
            import torch
            if torch.cuda.is_available() and not os.environ.get("AJBOT_NO_GPU"):
                from ajbot.model_utils import TorchMLP
                m["torch mlp (gpu)"] = TorchMLP(n_actions)
        except ImportError:
            pass                       # the laptop has no torch: not a candidate there
    return m


# Labels whose holdout recall is printed on their own: the overall score is
# dominated by attacks, and a model can gain a point there while forgetting
# how to block or back off - which is what loses matches against people.
RECALL_LABELS = ("Guard", "JustGuard", "Dodge", "Escape", "CounterAttack",
                 "DashBack", "DashForward", "Wait")


def _label_recall(pred, y, acts, name):
    pred = np.asarray(pred).astype(int).ravel()
    parts = []
    for a in RECALL_LABELS:
        if a not in acts:
            continue
        i = acts.index(a)
        n = int((y == i).sum())
        if not n:
            continue
        hit = int(((pred == i) & (y == i)).sum())
        said = int((pred == i).sum())
        parts.append(f"{a} {hit}/{n} ({hit/n:.0%}, predicted {said}x)")
    if parts:
        print(f"  recall by label ({name}):")
        for p in parts:
            print(f"    {p}")


def action_head(data, acts, chars, mode, pick_for=None, test_pick=None, outcomes=None,
                boost_picker=None, boost_mult=1.0):
    tr_files, te_files = data
    Xtr, ytr, wtr = arrays(tr_files, acts, chars, mode, pick_for, outcomes,
                           boost_picker, boost_mult)
    Xte, yte, _ = arrays(te_files, acts, chars, mode, test_pick)
    print(f"\n=== action head ({mode}) ===")
    print(f"  {len(Xtr)} train / {len(Xte)} holdout samples, "
          f"{Xtr.shape[1] if len(Xtr) else 0} features")
    if len(Xtr) < 60 or len(Xte) < 20:
        print("  too few samples to train")
        return
    maj = collections.Counter(ytr.tolist()).most_common(1)[0][0]
    base = float((yte == maj).mean())
    print(f"  train {len(Xtr)}  holdout {len(Xte)}")
    print(f"  reward weights: mean {wtr.mean():.2f}, "
          f"{100*(wtr > 1.5).mean():.0f}% of samples up-weighted, "
          f"{100*(wtr < 0.7).mean():.0f}% down-weighted")
    lab = collections.Counter(ytr.tolist())
    defence = {a: lab.get(acts.index(a), 0) for a in D.DEFENCE_LABELS.values()
               if a in acts}
    if defence:
        print(f"  defensive labels in training: {defence}")
    print(f"  {scrub.describe()}  (python -m ajbot.scrub reports what they drop)")
    print(f"  {'majority baseline':<22} holdout {base*100:5.1f}%  (always {acts[maj]})")
    hold = collections.Counter(yte.tolist())
    if len(hold) < 3 or base > 0.8:
        print(f"  !! holdout is degenerate: {[(acts[i], n) for i, n in hold.most_common()]}")
        print("     the tail of a single match is one repeated action, so this")
        print("     score is meaningless. Needs several matches, held out whole.")

    models = action_models(len(acts))
    best = (base, None)
    for name, clf in models.items():
        print(f"  fitting {name}...", end="", flush=True)
        fit_weighted(clf, Xtr, ytr, wtr)
        print("\r", end="")
        te = clf.score(Xte, yte)
        print(f"  {name:<22} train {clf.score(Xtr, ytr)*100:5.1f}%  holdout {te*100:5.1f}%")
        if te > best[0]:
            best = (te, (name, clf))
    print(f"  -> lift {(best[0]-base)*100:+.1f} points over baseline")
    if best[1]:
        _label_recall(best[1][1].predict(Xte), yte, acts, best[1][0])
    if mode == "frames":
        print("  NOTE: features include the current action, so this mostly")
        print("        measures persistence, not decision-making.")
    return best


def movement_models():
    """Candidate movement heads. The one that wins on the held-out match is
    the one save_policy ships, same as action_models()."""
    from sklearn.ensemble import RandomForestRegressor
    m = {
        # 120 trees / leaf 6: the 300-tree, leaf-2 forest was ~85% of the
        # policy file (500+ MB) and the slowest step of every live decision.
        # Leaf 4 -> 6 with FEATURE_VERSION 19: held-stick sampling gives ~1.6x
        # the rows (mostly near-repeats), and this keeps the forest the same size.
        "random forest": RandomForestRegressor(n_estimators=120,
                                               min_samples_leaf=6,
                                               random_state=0, n_jobs=-1),
        # The MLP regressor was dropped: on every run it scored worse than
        # predicting the mean (-158% on the last one) and dominated runtime.
    }
    if gpu_available():
        import xgboost as xgb
        m["xgboost (gpu)"] = xgb.XGBRegressor(
            n_estimators=400, max_depth=6, learning_rate=0.1, device="cuda",
            tree_method="hist", multi_strategy="multi_output_tree", random_state=0)
    return m


# The critic predicts, for a (situation, action) pair, what that choice
# itself causes: whether it connects within a second, how much damage it
# deals and how much the bot takes in the 1.5 s after it (dataset.
# decision_outcomes). Live play adds dealt - TAKEN_COST * taken to the cloned
# policy's preferences (agent.Agent._critique), so the bot leans toward
# choices that land and away from ones that get punished. A summed shaped
# return was tried first: it learned how good the situation was but could not
# tell actions apart (advantage sign at chance); these targets can (hit AUC
# ~0.8, ~0.6 between attacks, on a chronological holdout).
CRITIC_MIN_SAMPLES = 2000
TAKEN_COST = 1.2
LONG_WEIGHT = 0.6      # how much the 6 s view counts next to the 1.5 s one


def critic_inputs(X, y, n_actions):
    Z = np.zeros((len(X), X.shape[1] + n_actions), dtype=np.float32)
    Z[:, :X.shape[1]] = X
    Z[np.arange(len(y)), X.shape[1] + y] = 1.0
    return Z


def _xgb(classifier):
    import xgboost as xgb
    kw = dict(n_estimators=300, max_depth=5, learning_rate=0.05, subsample=0.8,
              colsample_bytree=0.5, tree_method="hist", random_state=0,
              device="cuda" if gpu_available() else "cpu")
    return xgb.XGBClassifier(**kw) if classifier else xgb.XGBRegressor(**kw)


def fit_critic(X, y, O, n_actions):
    """{'hit', 'dealt', 'taken'} models plus the scale of their combined score."""
    Z = critic_inputs(X, y, n_actions)
    heads = {"hit": _xgb(True).fit(Z, O[:, 0]),
             "dealt": _xgb(False).fit(Z, O[:, 1]),
             "taken": _xgb(False).fit(Z, O[:, 2])}
    if O.shape[1] >= 5:
        heads["dealt_long"] = _xgb(False).fit(Z, O[:, 3])
        heads["taken_long"] = _xgb(False).fit(Z, O[:, 4])
    for m in heads.values():
        m.set_params(device="cpu")
    return heads, float(outcome_score(O).std()) or 1.0


def outcome_score(O):
    s = O[:, 1] - TAKEN_COST * O[:, 2]
    if O.shape[1] >= 5:
        s = s + LONG_WEIGHT * (O[:, 3] - TAKEN_COST * O[:, 4])
    return s


def critic_score(heads, Z):
    s = heads["dealt"].predict(Z) - TAKEN_COST * heads["taken"].predict(Z)
    if "dealt_long" in heads:
        s = s + LONG_WEIGHT * (heads["dealt_long"].predict(Z) - TAKEN_COST * heads["taken_long"].predict(Z))
    return s


def critic_head(data, acts, chars):
    """Fit on the training matches and report on the held-out one."""
    from sklearn.metrics import roc_auc_score
    tr_files, te_files = data
    Xtr, ytr, Otr = arrays(tr_files, acts, chars, "outcomes")
    Xte, yte, Ote = arrays(te_files, acts, chars, "outcomes")
    print("\n=== critic (what each choice causes) ===")
    print(f"  {len(Xtr)} train / {len(Xte)} holdout decisions")
    if len(Xtr) < CRITIC_MIN_SAMPLES or len(Xte) < 50:
        print("  too little data for a critic")
        return None
    n = len(acts)
    heads, _ = fit_critic(Xtr, ytr, Otr, n)
    Zte = critic_inputs(Xte, yte, n)
    if 0 < Ote[:, 0].mean() < 1:
        print(f"  connects within 1 s: AUC {roc_auc_score(Ote[:, 0], heads['hit'].predict_proba(Zte)[:, 1]):.3f}")
    for col, name in ((1, "dealt"), (2, "taken")):
        g, pred = Ote[:, col], heads[name].predict(Zte)
        st = ((g - g.mean()) ** 2).sum() or 1.0
        print(f"  damage {name}: R^2 {1 - ((g - pred) ** 2).sum() / st:+.3f}")
    Z = critic_inputs(np.repeat(Xte, n, axis=0), np.tile(np.arange(n), len(Xte)), n)
    q = critic_score(heads, Z).reshape(len(Xte), n)
    adv = q[np.arange(len(Xte)), yte] - q.mean(axis=1)
    real = outcome_score(Ote)
    moved = np.abs(real - real.mean()) > 1e-3
    agree = float(((adv > 0) == (real > real.mean()))[moved].mean()) if moved.any() else float("nan")
    print(f"  chosen action better than average by the critic vs by the outcome: "
          f"{agree:.1%} agreement (50% = chance)")
    return heads


def movement_head(data, acts, chars, pick_for=None, test_pick=None, outcomes=None,
                  boost_picker=None, boost_mult=1.0):
    tr_files, te_files = data
    Xtr, Ytr, wtr = arrays(tr_files, acts, chars, "movement", pick_for, outcomes,
                           boost_picker, boost_mult)
    Xte, Yte, _ = arrays(te_files, acts, chars, "movement", test_pick)
    print(f"\n=== movement head ===")
    print(f"  {len(Xtr)} train / {len(Xte)} holdout samples")
    if len(Xtr) < 60 or len(Xte) < 20:
        print("  too few samples to train")
        return None

    mean = Ytr.mean(axis=0)
    base = float(np.mean((Yte - mean) ** 2))
    # Persistence is the baseline that matters: "keep pushing where you are
    # already pushing". sin/cos of the current stick are features 40 and 41.
    # lcam sin/cos sit right after the lang pair.
    cs, cc, cl = D.movement_persistence_cols(Xte.shape[1])
    persist = np.column_stack([Xte[:, cs], Xte[:, cc], Xte[:, cl]])
    pbase = float(np.mean((Yte - persist) ** 2))
    print(f"  train {len(Xtr)}  holdout {len(Xte)}")
    print(f"  {'predict-the-mean':<22} MSE {base:.4f}")
    print(f"  {'persistence':<22} MSE {pbase:.4f}   <- the one to beat")
    base = min(base, pbase)

    models = movement_models()
    fitted = {}
    best = (base, "random forest")
    for name, reg in models.items():
        print(f"  fitting {name}...", end="", flush=True)
        fit_weighted(reg, Xtr, Ytr, wtr)
        mse = float(np.mean((reg.predict(Xte) - Yte) ** 2))
        fitted[name] = reg
        print(f"\r  {name:<22} MSE {mse:.4f}   ({(1-mse/base)*100:+.1f}% vs mean)")
        if mse < best[0]:
            best = (mse, name)

    # Direction error in degrees is the interpretable number, for whichever
    # model actually won the holdout.
    winner = best[1]
    print(f"  -> shipping {winner} (best on the held-out match)")
    P = fitted[winner].predict(Xte)
    pa = np.arctan2(P[:, 0], P[:, 1])
    ta = np.arctan2(Yte[:, 0], Yte[:, 1])
    err = np.abs(np.rad2deg(np.arctan2(np.sin(pa - ta), np.cos(pa - ta))))
    print(f"  stick direction error: median {np.median(err):.0f}deg, "
          f"mean {err.mean():.0f}deg (random would be ~90)")
    return best[0], winner


def _is_bot(path):
    """True for the bot's own recordings, wherever they were globbed from.
    A recording seen from the human's side (SWAP_TAG) is not the bot's play."""
    if _swapped(path):
        return False
    return Path(path).resolve().parent.name == "botruns"


def permanent_defect(path, blind_limit=0.5):
    """Cached per recording (ajbot.tickcache) - see _permanent_defect."""
    from ajbot import tickcache
    n, reason = tickcache.cached_fact(path, f"defect{blind_limit:g}",
                                      lambda p: list(_permanent_defect(p, blind_limit)), raw=True)
    return n, reason


def _permanent_defect(path, blind_limit=0.5):
    """(tick count, reason) for why this recording can NEVER be trained on -
    reason is None if it can be.

    Unlike being short (a --min-ticks judgement call that can change), these
    are structural: a run where the `enemy` pointer is missing still yields
    samples - featurize fills the whole enemy half with zeros - so the model
    learns to attack from a blank picture of the opponent. An old-schema run
    carries no per-character movement fields at all. Neither gets fixed by
    recording more or waiting; the file itself needs archiving.

    Blindness is sampled every 25th line rather than parsed in full: the
    corpus is hundreds of MB of JSON and this runs before anything is cached.
    """
    import json as _json
    n = 0
    first = None
    blind = seen = 0
    frozen_seen = 0
    hp_vals = set()
    act_vals = set()
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            if first is None:
                first = _json.loads(line)
            if n % 25 == 0:
                t = _json.loads(line)
                if not t.get("summary"):
                    seen += 1
                    me, en, _, _ = D.resolve(t)
                    # Judged on the REPAIRED pointers: runs recorded under
                    # the old lever-pinning rule are 90-99% blind as written
                    # but recoverable (see dataset.resolve).
                    blind += not en
                    # A recording where the game-state hook got stuck (a
                    # frozen menu/win-screen the process kept logging through
                    # instead of stopping) reports the SAME hp and act for
                    # `me` on every single tick - real combat never holds
                    # both still this long. Caught here, at the same sampling
                    # cadence as blindness above, because it silently zeroes
                    # out the damage-taken/dealt reward for the whole file
                    # rather than raising any error.
                    if me:
                        c = (t.get("chars", {}) or {}).get(me) or {}
                        if "hp" in c or "act" in c:
                            frozen_seen += 1
                            hp_vals.add(c.get("hp"))
                            act_vals.add(c.get("act"))
            n += 1
    if not first:
        return n, "empty"
    if seen and blind / seen > blind_limit:
        return n, f"no opponent for {100*blind/seen:.0f}% of ticks"
    if frozen_seen >= 20 and len(hp_vals) <= 1 and len(act_vals) <= 1:
        return n, (f"frozen state - hp/act never changed across "
                   f"{frozen_seen} samples (stuck match/recording never stopped)")
    # Current schema carries per-character movement fields.
    if not any("lang" in c for c in first.get("chars", {}).values()):
        return n, "old schema, no movement fields"
    return n, None


# A bot recording is one match (play.py / self-play), ~600-1300 ticks when it
# ran to the end; --min-ticks (2000) is sized for record.py sessions and
# silently dropped every one of them, so league challengers were refits on
# the same human data. Only aborted bot matches fall under this.
BOT_MIN_TICKS = 400


def _is_drill(path):
    """A drill recording (practice.py --record into drillruns/)."""
    return Path(path).resolve().parent.name == "drillruns"


# A drill file holds only landed attempts (~2-4 s each): far shorter than a
# match, and nothing in it is an aborted game.
DRILL_MIN_TICKS = 60


def _usable(path, min_ticks, blind_limit=0.5):
    """Skip short recordings and any with a permanent_defect()."""
    n, defect = permanent_defect(path, blind_limit)
    if defect:
        print(f"  skipping {path}: {defect}")
        return False
    if _is_drill(path):
        return n >= DRILL_MIN_TICKS
    return n >= (min(min_ticks, BOT_MIN_TICKS) if _is_bot(path) else min_ticks)


# Cherrypicking already keeps only the stretches of a self-play recording
# that landed something; this scales the whole file's weight by whether that
# side actually won, which cherrypicking says nothing about - a beautiful
# combo in a match the bot went on to lose is still worth imitating, just a
# little less than the same combo in a match it won.
OUTCOME_WEIGHT = {"win": 1.3, "loss": 0.8}

# Scales a whole file's weight by who was actually playing, on top of
# OUTCOME_WEIGHT and --execution-boost. A human or the game's own CPU on
# either side plays like something the model will actually face; two
# instances of our own bot mirroring each other can drift into exploiting
# its own blind spots instead, so pure self-play is worth less per tick, not
# excluded - it is still real combo/timing data.
MATCH_KIND_WEIGHT = {
    "human": 1.2,      # runs/ - a human played "me" (record.py), any opponent
    "vs_cpu": 1.2,      # botruns/, solo file, opponent is the game's own CPU
    "vs_human": 1.2,    # botruns/, solo file, opponent is a real human on a pad
    "self_play": 0.5,   # botruns/, paired <ts>.<tag>.jsonl - both sides our bot
    # A vs_human file seen from the human's side (SWAP_TAG, D.swap_sides):
    # the human's own decisions against the bot. These are the players the
    # bot loses to (the 2026-11-09 pro session), so they count the most.
    "human_swapped": 1.5,
    # drillruns/ (practice.py --record): the bot's own LANDED attempts at a
    # library combo against a training dummy - whole long routes, which
    # matches almost never show, but off a standing start, so not more.
    "drill": 1.0,
}

# Kinds where a real person was playing. --human-share scales their files up
# (never down) until they carry at least that share of the total sample
# weight: self-play grows ~100 matches a generation, human data does not, so
# fixed multipliers alone let it fade. (Measured 2026-11-11: 43% of the
# weight with cherrypicking on; the league asks for 60%.)
HUMAN_KINDS = ("human", "vs_human", "human_swapped")
HUMAN_SHARE = 0.0


def rebalance(kinds, ws, share):
    """Scale the human files' weights so they are >= `share` of the total.
    `kinds` and `ws` are per file, aligned; changes `ws` in place."""
    if not share:
        return
    h = sum(float(w.sum()) for k, w in zip(kinds, ws) if k in HUMAN_KINDS)
    o = sum(float(w.sum()) for k, w in zip(kinds, ws) if k not in HUMAN_KINDS)
    if h <= 0 or o <= 0:
        return
    if h / (h + o) >= share:
        print(f"  human share {h / (h + o):.0%} (already >= {share:.0%})")
        return
    k = share * o / ((1 - share) * h)
    for i, kind in enumerate(kinds):
        if kind in HUMAN_KINDS:
            ws[i] = ws[i] * k
    print(f"  human share {h / (h + o):.0%} -> {share:.0%} (human weight x{k:.2f})")


# A vs_human recording listed a second time with this suffix is trained on
# from the human's side. The suffix never reaches the filesystem: _src()
# strips it everywhere a path is opened, classified or keyed.
SWAP_TAG = "|swap"


def _src(f):
    return f[:-len(SWAP_TAG)] if f.endswith(SWAP_TAG) else f


def _swapped(f):
    return f.endswith(SWAP_TAG)


def _opponent_ai_fraction(path, sample_every=25):
    from ajbot import tickcache
    return tickcache.cached_fact(path, f"aifrac{sample_every}",
                                 lambda p: _opponent_ai_fraction_raw(p, sample_every), raw=True)


def _opponent_ai_fraction_raw(path, sample_every=25):
    """Fraction of sampled ticks where the enemy read ai=1 - see
    spectator.opponent_is_cpu, which does the same thing over fully-loaded
    ticks. A real CPU opponent reads ai=1 on the clear majority of ticks; a
    human or another bot essentially never does. Sampled the same way
    permanent_defect() samples for blindness, so classifying a file costs a
    partial read, not a full D.load() that would defeat the point of the
    build cache for every file that ends up a cache hit.
    """
    import json as _json
    total = ai_votes = 0
    with open(path, encoding="utf-8") as fh:
        for i, line in enumerate(fh):
            if not line.strip() or i % sample_every != 0:
                continue
            t = _json.loads(line)
            if t.get("summary"):
                continue
            _, en, _, _ = D.resolve(t)
            if not en:
                continue
            c = (t.get("chars", {}) or {}).get(en) or {}
            total += 1
            if c.get("ai"):
                ai_votes += 1
    return (ai_votes / total) if total else 0.0


def _match_kind(path):
    """Classify a recording by who was actually playing, not just which
    directory it came from: botruns/ holds both play.py's solo-per-match
    recordings (the bot plays one side; the OTHER side is either the game's
    own CPU or a real human on a pad) and play2.py's self-play (a paired
    "<timestamp>.<bot-tag>.jsonl" per side, both from the SAME match, neither
    side a human or the real CPU). A paired filename (play2.py's naming,
    unlike play.py's plain "<timestamp>.jsonl") tells self-play apart from a
    human on the other pad; the enemy's `ai` flag tells CPU apart from both.
    """
    if _swapped(path):
        return "human_swapped"
    if _is_drill(path):
        return "drill"
    if not _is_bot(path):
        return "human"
    paired = Path(path).stem.count(".") >= 1
    if _opponent_ai_fraction(path) >= 0.5:
        return "vs_cpu"
    return "self_play" if paired else "vs_human"


def _handicapped(path):
    """A match the bot played below full strength (ajbot.skillmatch: every
    tick carries "skill" < 1). Its own side is not imitated."""
    from ajbot import tickcache
    return tickcache.cached_fact(path, "skill1", _handicapped_raw, raw=True)


def _handicapped_raw(path):
    import json as _json
    with open(path, encoding="utf-8") as fh:
        for i, line in enumerate(fh):
            if i > 200:
                break
            if not line.strip():
                continue
            v = _json.loads(line).get("skill")
            if isinstance(v, (int, float)):
                return v < 0.95
    return False


# Self-play where one bot spent the match standing still teaches nothing about
# fighting: every "combo" was a free hit. Judged over the whole file (the
# per-hit free_hit discount in dataset.rewards handles the rest), a run's
# weight falls from 1 at PASSIVE_OK to PASSIVE_FLOOR at PASSIVE_BAD.
PASSIVE_ACTS = ("Idle", "None", "Land", "Wait", "Stand", "Landing")
PASSIVE_OK, PASSIVE_BAD, PASSIVE_FLOOR = 0.30, 0.60, 0.2


def _passive_fraction(path, sample_every=4):
    from ajbot import tickcache
    return tickcache.cached_fact(path, f"passive{sample_every}",
                                 lambda p: _passive_fraction_raw(p, sample_every), raw=True)


def _passive_fraction_raw(path, sample_every=4):
    """Share of the opponent's free (not in hitstun/guard) sampled ticks that
    it spent standing, in a recording."""
    import json as _json
    n = p = 0
    with open(path, encoding="utf-8") as fh:
        for i, line in enumerate(fh):
            if not line.strip() or i % sample_every:
                continue
            t = _json.loads(line)
            if t.get("summary") or "chars" not in t:
                continue
            _, en, _, _ = D.resolve(t)
            a = ((t["chars"].get(en) or {}).get("act")) if en else None
            if not a or a.startswith(("Damage", "WinDemo", "Appearance")) or "Guard" in a:
                continue
            n += 1
            p += a in PASSIVE_ACTS
    return p / n if n > 30 else 0.0


def passive_weight(path):
    f = _passive_fraction(path)
    if f <= PASSIVE_OK:
        return 1.0
    if f >= PASSIVE_BAD:
        return PASSIVE_FLOOR
    return 1.0 - (1.0 - PASSIVE_FLOOR) * (f - PASSIVE_OK) / (PASSIVE_BAD - PASSIVE_OK)


def suspect_result(d):
    """A recorded result that cannot be right. Sessions that started without
    pinned sides sometimes put both teams on the bot's side; the opponent's
    victory pose then reads as a bot win with nothing dealt and the bot's own
    team KO'd (seen as 'win', dealt 0.00, taken 5+). Such files would be
    up-weighted as wins."""
    if d.get("result") == "win" and (d.get("damage_dealt") or 0) < 0.05 \
            and (d.get("damage_taken") or 0) > 1.0:
        return True
    return False


def _load_outcomes(stats_path="stats/matches.jsonl"):
    """{resolved recording path: result} from stats/matches.jsonl.

    Only about half of self-play recordings end up with a linked result (a
    stopped or abandoned run has none), so this is a bonus signal where it
    exists, not something everything depends on.
    """
    import json as _json
    out = {}
    p = Path(stats_path)
    if not p.exists():
        return out
    with p.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                d = _json.loads(line)
            except ValueError:
                continue
            rec, result = d.get("recording"), d.get("result")
            if suspect_result(d):
                continue
            if rec and result in OUTCOME_WEIGHT:
                # PureWindowsPath splits on both separators: the stats
                # file's paths are Windows-style, and training may run on Linux.
                from pathlib import PureWindowsPath
                p = PureWindowsPath(rec)
                out[f"{p.parent.name}/{p.name}"] = result
    return out


def _build_one(args):
    """Worker: cached (X, y, w) for one recording. Module-level so it can be
    pickled and run in a subprocess (each file's build/cache is independent).

    `file_mult` (MATCH_KIND_WEIGHT x OUTCOME_WEIGHT, from arrays()'s
    mult_for()) is applied AFTER the cache lookup, not baked into it, so the
    cached arrays stay the natural per-tick reward weights and retuning
    either table later does not require a fingerprint bump. `boost_picker`
    can't be applied after the fact the same way - which ticks it marks
    depends on the recording itself - so it joins the cache key instead,
    same as the filtering `pick`.
    """
    path, acts, chars, kind, pick, file_mult, boost_picker, boost_mult, strict = args
    from ajbot import cache
    swap = _swapped(path)
    path = _src(path)

    key = kind
    if swap:
        key += "@swap"
    if strict:
        key += "@strict"
    # The bot's own escape presses are EscapePolicy's hand rule, not a
    # learned choice; the head learns Escape from humans (incl. swapped).
    drop = ("Escape",) if (kind not in ("outcomes", "movement") and Path(path).resolve().parent.name == "botruns"
                           and not swap) else ()
    if drop:
        key += "@noesc"
    if pick is not None:
        key += f"@{pick.tag}"
    if boost_picker is not None:
        # No colons: this joins a filename (see cache._entry), and ':' is
        # invalid there on Windows.
        key += f"@boost={boost_picker.tag}x{boost_mult:g}"

    def fn(ticks):
        if swap:
            ticks = D.swap_sides(ticks)
        keep = pick.keep(ticks) if pick else None
        boost = boost_picker.keep(ticks) if boost_picker else None
        if kind == "outcomes":
            return D.build_weighted(ticks, acts, chars, mode="transitions", return_outcomes=True)
        if kind == "movement":
            return D.build_movement_weighted(ticks, acts, chars, keep=keep,
                                             boost=boost, boost_mult=boost_mult,
                                             strict=strict)
        return D.build_weighted(ticks, acts, chars, mode=kind, keep=keep,
                                boost=boost, boost_mult=boost_mult, strict=strict,
                                drop_labels=drop)

    X, y, w = cache.load_or_build(path, acts, chars, key, fn)
    if kind == "outcomes":
        return X, y, w                    # targets here, never scaled
    if file_mult != 1.0 and len(w):
        w = w * file_mult
    return X, y, w


def arrays(files, acts, chars, kind, pick_for=None, outcomes=None,
          boost_picker=None, boost_mult=1.0):
    """Cached (X, y) for a list of recordings, concatenated.

    `pick_for(path)` returns a combos.Picker for recordings that should be
    cherry-picked, or None to use the whole run. The picker's tag joins the
    cache key, so a filtered build and a full one never share an entry.

    `outcomes` is the {path: result} map from _load_outcomes(); only bot
    recordings (see _is_bot) are scaled by it. Every file, bot or human, is
    also scaled by MATCH_KIND_WEIGHT (see _match_kind) - who was actually
    playing, not just which directory the file came from.

    `boost_picker`, if given, up-weights (not filters - see
    build_weighted's `boost`) every file's combo-execution stretches by
    `boost_mult`, human and bot recordings alike: unconditional "landed
    something" execution generalizes past whichever opponent was recorded,
    unlike the read-heavy neutral exchanges that make up most of a match
    (see train.py's --execution-boost).

    Each file's cache lookup/build is independent, so they run across a
    process pool rather than one at a time on a single core.
    """
    outcomes = outcomes or {}

    def mult_for(f):
        kind_f = _match_kind(f)
        if not _swapped(f) and _is_bot(f) and _handicapped(f):
            return 0.0    # the bot played down on purpose; the human side still counts
        m = MATCH_KIND_WEIGHT.get(kind_f, 1.0)
        if kind_f == "self_play":
            m *= passive_weight(f)
        src = Path(_src(f))
        result = outcomes.get(f"{src.parent.name}/{src.name}")
        if _swapped(f):
            # The bot's result, seen from the human's side.
            result = {"win": "loss", "loss": "win"}.get(result)
            m *= OUTCOME_WEIGHT.get(result, 1.0)
        elif _is_bot(f):
            m *= OUTCOME_WEIGHT.get(result, 1.0)
        return m

    file_kinds = [_match_kind(f) for f in files]
    jobs = [(f, acts, chars, kind, pick_for(f) if pick_for else None, mult_for(f),
             boost_picker, boost_mult, fk == "self_play")
            for f, fk in zip(files, file_kinds)]
    Xs, ys, ws, kinds = [], [], [], []
    if len(jobs) > 1:
        # Each worker fully loads its file (D.load), so this is sized against
        # free RAM, not just cores - see ajbot.parallel for why.
        sizes = tickcache.src_sizes([_src(f) for f in files])
        n_workers = safe_worker_count(sizes, WORKERS)
        with ProcessPoolExecutor(max_workers=n_workers) as ex:
            results = list(ex.map(_build_one, jobs))
    else:
        results = [_build_one(j) for j in jobs]
    for fk, (X, y, w) in zip(file_kinds, results):
        if len(X):
            Xs.append(X)
            ys.append(y)
            ws.append(w)
            kinds.append(fk)
    if kind != "outcomes":
        rebalance(kinds, ws, HUMAN_SHARE)
    if not Xs:
        return (np.zeros((0, 0), np.float32), np.zeros((0,), np.int64),
                np.zeros((0,), np.float32))
    return np.concatenate(Xs), np.concatenate(ys), np.concatenate(ws)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", default=None, help="glob; default = all runs")
    ap.add_argument("--mode", default="transitions", choices=["transitions", "frames"])
    ap.add_argument("--min-ticks", type=int, default=2000,
                    help="ignore short/aborted recordings")
    ap.add_argument("--save", metavar="DIR", default=None,
                    help="fit on ALL matches and save a policy there")
    ap.add_argument("--human-share", type=float, default=0.0,
                    help="scale human recordings up to at least this share of "
                         "the total sample weight (0 = off; the league uses 0.6)")
    ap.add_argument("--drills", default="drillruns/*.jsonl",
                    help="with --include-bot: glob of landed combo-drill recordings "
                         "(practice.py --record); '' to leave them out")
    ap.add_argument("--include-bot", action="store_true",
                    help="also train on the bot's own runs (see the warning)")
    ap.add_argument("--cherrypick", nargs="?", const="bot", default="off",
                    choices=["off", "bot", "all"],
                    help="train only on stretches that actually landed. "
                         "'bot' (the default when the flag is given) filters "
                         "the bot's own runs and keeps human runs whole; "
                         "'all' filters both")
    ap.add_argument("--min-damage", type=float, default=0.05,
                    help="cherrypick: least damage a combo must deal")
    ap.add_argument("--min-hits", type=int, default=2,
                    help="cherrypick: least hits a combo must land")
    ap.add_argument("--top-frac", type=float, default=0.5,
                    help="cherrypick: fraction of each character's combos to "
                         "keep, ranked within that character")
    ap.add_argument("--per-char-cap", type=int, default=None,
                    help="cherrypick: hard cap on combos kept per character")
    ap.add_argument("--lead-s", type=float, default=None,
                    help="cherrypick: seconds of set-up kept before a combo")
    ap.add_argument("--execution-boost", type=float, default=1.0,
                    help="extra sample weight (multiplier) for combo-execution "
                         "stretches, identified the same way as --cherrypick but "
                         "WITHOUT discarding anything else - applies to human and "
                         "bot recordings alike. 1.0 = off")
    ap.add_argument("--no-cache", action="store_true", help="ignore the feature cache")
    ap.add_argument("--quick", action="store_true",
                    help="skip the held-out comparison of candidate models and fit "
                         "the usual winners (xgboost actions, random-forest movement) "
                         "straight away - much faster; use without it now and then "
                         "to check the winners have not changed")
    ap.add_argument("--warm-only", action="store_true",
                    help="build every cache (digests, features, outcomes) and stop")
    ap.add_argument("--no-critic", action="store_true",
                    help="skip the action-value critic (pure behaviour cloning)")
    ap.add_argument("--no-swap", action="store_true",
                    help="do not also learn from the human's side of vs_human "
                         "recordings (see D.swap_sides)")
    ap.add_argument("--no-wait", action="store_true",
                    help="leave the Wait label out of the vocabulary")
    ap.add_argument("--holdout", default="humans", choices=["humans", "last"],
                    help="model comparison holdout: 'humans' = every vs_human "
                         "match, scored on the human's own decisions (falls "
                         "back to 'last' without them); 'last' = the newest "
                         "human run")
    args = ap.parse_args()
    global HUMAN_SHARE
    HUMAN_SHARE = args.human_share

    files = sorted(glob.glob(args.runs or "runs/*.jsonl"))
    if args.include_bot:
        # Cloning the bot's own play is a feedback loop: it reinforces what it
        # already does rather than what good play looks like. Only useful once
        # runs are filtered by outcome.
        files += sorted(glob.glob("botruns/*.jsonl"))
        print("WARNING: training on the bot's own runs clones its current "
              "behaviour, not yours")
    if args.include_bot and args.drills:
        # The bot's own play, like botruns/: never in a human-only fit
        # (league.py's models/human, the boss's lean).
        drills = sorted(glob.glob(args.drills))
        if drills:
            files += drills
            print(f"  {len(drills)} drill recordings ({args.drills})")
    if len(files) > 1:
        with ProcessPoolExecutor(max_workers=min(WORKERS, len(files))) as ex:
            usable = list(ex.map(partial(_usable, min_ticks=args.min_ticks), files))
        files = [f for f, ok in zip(files, usable) if ok]
    else:
        files = [f for f in files if _usable(f, args.min_ticks)]

    pick_for = None
    if args.cherrypick != "off":
        from ajbot.combos import Picker
        from ajbot import combos as _C
        picker = Picker(min_damage=args.min_damage, min_hits=args.min_hits,
                        top_frac=args.top_frac, per_char_cap=args.per_char_cap,
                        lead_s=_C.LEAD_S if args.lead_s is None else args.lead_s)
        if args.cherrypick == "all":
            pick_for = lambda f: picker
        else:
            # Human recordings are the scarce, trusted signal - filtering them
            # throws away the only examples not produced by the bot itself.
            # The same goes for the bot's matches against a human: keeping
            # only its landed combos kept 1-11% of the 2026-11-09 pro session
            # (Guard 19 of 180, Dash 0 of 35) - the defence and footsies it
            # most needs to learn from were exactly what got cut.
            pick_for = lambda f: (picker if _is_bot(f) and _match_kind(f) != "vs_human"
                                  else None)
        print(f"cherrypicking ({args.cherrypick}) with {picker.tag}")

    boost_picker = None
    if args.execution_boost != 1.0:
        from ajbot.combos import Picker
        from ajbot import combos as _C
        boost_picker = Picker(min_damage=args.min_damage, min_hits=args.min_hits,
                              top_frac=args.top_frac, per_char_cap=args.per_char_cap,
                              lead_s=_C.LEAD_S if args.lead_s is None else args.lead_s)
        print(f"execution boost: x{args.execution_boost:g} on combo stretches "
              f"identified by {boost_picker.tag} (human and bot alike, nothing discarded)")

    if args.no_cache:
        from ajbot import cache
        cache.clear()
    if len(files) < 2:
        print(f"need at least 2 matches for a held-out-match split; found {len(files)}")
        return

    # Hold out a HUMAN match when there is one. Bot runs sort after human runs,
    # so taking files[-1] blindly meant that switching on --include-bot quietly
    # started scoring the policy against the bot's own play - which it can
    # reproduce by definition. The human run is the reference worth matching.
    vs_h = [f for f in files if _match_kind(f) == "vs_human"]
    swaps = [] if args.no_swap else [f + SWAP_TAG for f in vs_h]
    if swaps:
        print(f"{len(swaps)} vs_human matches also learned from the human's side")
    if args.holdout == "humans" and swaps and len(vs_h) < len(files):
        # Score candidates on what humans did against the bot - the play it
        # has to learn - with those matches kept out of training entirely
        # (both sides), so no situation leaks across. A single held-out
        # human run was 300 decisions; this is every human-vs-bot match.
        te_files = swaps
        tr_files = [f for f in files if f not in vs_h]
        print(f"{len(files)} matches; holding out all {len(vs_h)} vs_human "
              f"matches, scored on the human's decisions")
    else:
        human = [f for f in files if not _is_bot(f) and not _is_drill(f)]
        holdout = human[-1] if (human and len(human) < len(files)) else files[-1]
        tr_files = [f for f in files if f != holdout] + swaps
        te_files = [holdout]
        print(f"{len(files)} matches; holding out {holdout}")

    # The holdout is never cherry-picked. Filtering it would score the policy
    # only on the moments that already went well, which is the question nobody
    # is asking - and on one match it leaves a couple of dozen samples.
    test_pick = None

    from ajbot import cache
    if len(files) > 1:
        # A cache miss here also fully loads the file (D.load) - same
        # memory-vs-cores sizing as the feature-build pool.
        sizes = tickcache.src_sizes(files)
        n_workers = safe_worker_count(sizes, WORKERS)
        with ProcessPoolExecutor(max_workers=n_workers) as ex:
            counts = list(ex.map(cache.vocab_counts, files))
        acts_c, chars_c = {}, {}
        for a, c in counts:
            for k, v in a.items():
                acts_c[k] = acts_c.get(k, 0) + v
            for k, v in c.items():
                chars_c[k] = chars_c.get(k, 0) + v
        order = lambda d: [k for k, _ in sorted(d.items(), key=lambda kv: -kv[1])]
        acts, chars = order(acts_c), order(chars_c)
    else:
        acts, chars = cache.merged_vocab(files)
    if not args.no_wait and D.WAIT not in acts:
        acts.append(D.WAIT)
    files = files + swaps       # everything below (warm, save) uses both sides
    print(f"{len(tr_files)} train matches, {len(te_files)} holdout")
    print(f"{len(acts)} actions, {len(chars)} characters")
    outcomes = _load_outcomes()
    if outcomes:
        print(f"{len(outcomes)} recordings have a linked win/loss result "
              f"and will be weighted by it")
    if args.warm_only:
        import time as _t
        t0 = _t.time()
        for kind in (args.mode, "movement"):
            arrays(files, acts, chars, kind, pick_for, outcomes, boost_picker, args.execution_boost)
        if not args.no_critic:
            arrays(files, acts, chars, "outcomes")
        print(f"caches warm ({_t.time() - t0:.0f}s)")
        return
    if args.quick:
        best = (None, ("xgboost (gpu)" if gpu_available() else "xgboost (cpu)",))
        move_best = ("random forest", "random forest")
        print("quick: skipping the holdout comparison")
    else:
        best = action_head((tr_files, te_files), acts, chars, args.mode, pick_for,
                           test_pick, outcomes, boost_picker, args.execution_boost)
        move_best = movement_head((tr_files, te_files), acts, chars, pick_for,
                                  test_pick, outcomes, boost_picker, args.execution_boost)
        if not args.no_critic:
            critic_head((tr_files, te_files), acts, chars)

    if args.save:
        kind = best[1][0] if best and best[1] else "logistic regression"
        move_kind = move_best[1] if move_best else "random forest"
        print(f"\nshipping the {kind} action head (best on the held-out match)")
        save_policy(args.save, files, acts, chars, args.mode, pick_for,
                    action_kind=kind, movement_kind=move_kind, outcomes=outcomes,
                    boost_picker=boost_picker, boost_mult=args.execution_boost,
                    critic=not args.no_critic)


KEEP_POLICIES = 3       # a policy is ~380 MB, so this is not free


def _rotate(path, keep=KEEP_POLICIES):
    """Move the existing policy aside before it is overwritten.

    Retraining is destructive otherwise, and the previous policy is worth
    having: play2.py --model-b runs the new one against it, which is the only
    direct read on whether a retrain actually improved anything.
    """
    import shutil
    import time as _time
    if not path.exists():
        return
    prev = path.parent / "prev"
    prev.mkdir(parents=True, exist_ok=True)
    stamp = _time.strftime("%Y%m%d-%H%M%S")
    dest = prev / f"{path.stem}-{stamp}{path.suffix}"
    shutil.move(str(path), str(dest))
    old = sorted(prev.glob(f"{path.stem}-*{path.suffix}"))
    for stale in old[:-keep]:
        stale.unlink(missing_ok=True)
    mb = sum(f.stat().st_size for f in prev.glob("*")) / 1e6
    print(f"  previous policy -> {dest}  ({mb:.0f} MB kept in {prev})")


def save_policy(out_dir, files, acts, chars, mode, pick_for=None,
                action_kind="logistic regression", movement_kind="random forest",
                outcomes=None, boost_picker=None, boost_mult=1.0, critic=True):
    """Refit on every match (no holdout) and persist for live play."""
    import pickle
    from pathlib import Path

    Xa, ya, wa = arrays(files, acts, chars, mode, pick_for, outcomes,
                        boost_picker, boost_mult)
    Xm, Ym, wm = arrays(files, acts, chars, "movement", pick_for, outcomes,
                        boost_picker, boost_mult)
    print(f"\nfitting policy on all data: {len(Xa)} action, {len(Xm)} movement")

    def _to_cpu(model):
        # Trained on the GPU, but live play predicts one tick at a time and
        # would otherwise pay a device-mismatch penalty every decision - and
        # contend with the game itself for the GPU while playing.
        if hasattr(model, "set_params") and "device" in getattr(model, "get_params", dict)():
            model.set_params(device="cpu")
        return model

    print(f"  fitting action head ({action_kind})...", end="", flush=True)
    action = _to_cpu(fit_weighted(action_models(len(acts))[action_kind], Xa, ya, wa))
    print(" done")
    print(f"  fitting movement head ({movement_kind})...", end="", flush=True)
    movement = _to_cpu(fit_weighted(movement_models()[movement_kind], Xm, Ym, wm))
    print(" done")
    critic_m, critic_scale, critic_counts = None, None, {}
    if critic:
        Xc, yc, Oc = arrays(files, acts, chars, "outcomes")
        if len(Xc) >= CRITIC_MIN_SAMPLES:
            print(f"  fitting critic on {len(Xc)} decisions...", end="", flush=True)
            critic_m, critic_scale = fit_critic(Xc, yc, Oc, len(acts))
            print(" done")
            # How often the critic saw each action taken: live play can
            # trust its opinion of a common action and discount one it has
            # barely any outcomes for.
            critic_counts = {acts[int(i)]: int(n)
                             for i, n in collections.Counter(yc.tolist()).items()}

    d = Path(out_dir)
    d.mkdir(parents=True, exist_ok=True)
    _rotate(d / "policy.pkl")
    with (d / "policy.pkl").open("wb") as fh:
        pickle.dump({"action": action, "movement": movement,
                     "actions": acts, "chars": chars, "mode": mode,
                     # recorded so a stale policy fails with a clear message
                     # instead of a shape error deep inside sklearn
                     "n_features": int(Xa.shape[1]),
                     "critic": critic_m, "critic_scale": critic_scale,
                     "critic_counts": critic_counts,
                     # the route-progress features' book, so live play
                     # matches what the policy learned (dataset.feature_book)
                     "route_book": {c: [list(q) for q in v]
                                    for c, v in D.feature_book().items()}}, fh)
    print(f"saved {d / 'policy.pkl'}")


if __name__ == "__main__":
    main()
