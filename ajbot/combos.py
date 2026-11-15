"""Find, score and cherry-pick the combos inside a recording.

Behaviour cloning copies whatever it is shown. Human recordings are scarce and
the bot can generate its own indefinitely, but training on a raw bot run clones
its average play - including every whiff and every neutral shuffle, which is
most of a match. What is worth cloning is the minority of moments where
something actually landed.

So a run is cut into COMBOS - stretches where the opponent was taking damage
without getting away - each is scored, and only the ones that paid off are fed
back. The rest of the run still happened; it just is not held up as an example.

Two measurements decided the thresholds, both taken across all 41 human runs
and 16 bot runs:

* Inter-hit gaps are bimodal. Half of all hits land within 0.2s of the previous
  one and the curve flattens out past ~1s, so 1s is the break between "still
  the same combo" and "a new engagement". Both sources agree on the knee.
* Recordings are NOT a fixed rate - human runs sample at 18-60 Hz and bot runs
  at 10-15 Hz - so every window here is in SECONDS, off the tick's own `t`.
  Counting ticks would make a combo mean something different per file.

Damage is read from health rates on both sides rather than from the `me`/
`enemy` pointers, because a tag mid-combo changes who those point at. A combo
that runs through a tag chain is credited to every character that swung during
it, which is the point of tagging in the first place.
"""
import collections

BREAK_S = 1.0        # silence this long ends a combo (measured knee)
LEAD_S = 1.0         # seconds of set-up kept before the first hit
MIN_HIT = 0.0005     # below this is float noise, not a hit
MAX_HIT = 0.5        # above this is a KO or a match reset, not one hit
RISE_TICKS = 8       # consecutive ticks above the floor before a rise is real
UNNAMED = "unnamed"  # characters that ship no Lua namespace, pooled together


class Health:
    """Damage on the monotone envelope, per character.

    A raw tick-to-tick delta is NOT usable. Some characters own more than one
    live lua_State at the same age - Bakugo, Nomu and Eraserhead among them -
    and sampler.dedupe breaks that tie arbitrarily, so the recorded health
    flips between two values every few ticks. Counting each dip as a hit turned
    one Bakugo sequence into a "209-hit, 66-damage combo" when a whole match
    only deals 3 to 6. It shows up in 19 of the 53 recordings on disk.

    So health is only ever believed downward. A reading above the floor is
    ignored unless it holds for RISE_TICKS in a row, which a two-state flip
    never manages but a real heal or a round reset does.

    The same flaw is in TagPolicy/EscapePolicy, which count hits off the raw
    delta - worth fixing there too, but that changes live behaviour.
    """

    def __init__(self, rise_ticks=RISE_TICKS):
        self.rise_ticks = rise_ticks
        self.floor = {}
        self.up = {}

    def drop(self, key, hp):
        """Damage taken by `key` this tick, 0 when the reading is not believed."""
        floor = self.floor.get(key)
        if floor is None:
            self.floor[key] = hp
            return 0.0
        if hp <= floor:
            d = floor - hp
            self.floor[key] = hp
            self.up[key] = 0
            return d if MIN_HIT < d < MAX_HIT else 0.0
        n = self.up.get(key, 0) + 1
        self.up[key] = n
        if n >= self.rise_ticks:
            self.floor[key] = hp     # a real heal, or a new round
            self.up[key] = 0
        return 0.0


class Combo:
    """One stretch of sustained offence, with what it cost and what it paid."""

    __slots__ = ("start_i", "end_i", "start_t", "end_t", "first_hit_t",
                 "last_hit_t", "dealt", "taken", "hits", "attackers",
                 "defenders", "actions", "tags", "resources", "escapes")

    def __init__(self, i, t):
        self.start_i = self.end_i = i
        self.start_t = self.end_t = self.first_hit_t = self.last_hit_t = t
        self.dealt = self.taken = 0.0
        self.hits = 0
        self.attackers = []          # ordered, one entry per character that swung
        self.defenders = []
        self.actions = []            # collapsed command sequence
        self.tags = self.resources = self.escapes = 0

    # ---- derived ----
    @property
    def duration(self):
        return max(self.last_hit_t - self.first_hit_t, 0.0)

    @property
    def net(self):
        """Damage dealt minus damage taken while it ran."""
        return self.dealt - self.taken

    @property
    def dps(self):
        d = self.duration
        return self.dealt / d if d > 0.05 else self.dealt

    @property
    def character(self):
        """Who started it - the character this combo is credited to."""
        return self.attackers[0] if self.attackers else None

    @property
    def sequence(self):
        return tuple(self.actions)

    def __repr__(self):
        return (f"<Combo {short(self.character)} {self.hits}hit "
                f"{self.dealt:.3f}dmg net{self.net:+.3f} "
                f"{self.duration:.1f}s {'/'.join(short(a) for a in self.attackers)}>")


def short(key):
    """'Pl02Bakugo' -> 'Bakugo'. Anything with no namespace is UNNAMED.

    22 of 113 characters ship no Lua namespace and are recorded as '?' or a
    raw slot handle. They cannot be told apart from one another, so they share
    a single bucket - and that bucket must never be treated as a character. A
    repertoire built from it would merge several fighters' sequences into one
    combo list that belongs to nobody.
    """
    if not key or not key.startswith("Pl"):
        return UNNAMED
    return key.split("#", 1)[0][2:].lstrip("0123456789") or UNNAMED


def _sides(tick):
    """(mine, theirs) as key sets for one tick.

    Taken from dataset.resolve, which repairs runs recorded under the old
    lever-pinning rule (the CPU's fighters were written into `mine`) and falls
    back to the per-record `enemy` flag for recordings with no lists at all.
    """
    from .dataset import resolve
    me, en, mine, theirs = resolve(tick)
    mine, theirs = set(mine), set(theirs)
    if me:
        mine.add(me)
    if en:
        theirs.add(en)
    return mine, theirs


def find(ticks, break_s=BREAK_S, lead_s=LEAD_S):
    """Every combo in a loaded recording, in order.

    Health is tracked per character across the whole run, so a tag on either
    side keeps its damage attributed instead of resetting when the `me` or
    `enemy` pointer moves.
    """
    out = []
    health = Health()
    cur = None
    # (index, time) history, so a combo can reach back over its own set-up.
    times = []

    for i, tick in enumerate(ticks):
        if tick.get("summary"):
            continue
        now = tick.get("t")
        if not isinstance(now, (int, float)):
            continue
        times.append((i, now))
        mine, theirs = _sides(tick)
        chars = tick.get("chars") or {}

        dealt = taken = 0.0
        for key, rec in chars.items():
            hp = rec.get("hp")
            if not isinstance(hp, (int, float)):
                continue
            drop = health.drop(key, hp)
            if drop <= 0.0:
                continue
            if key in theirs:
                dealt += drop
            elif key in mine:
                taken += drop

        # A long enough silence ends whatever was running.
        if cur is not None and now - cur.last_hit_t > break_s:
            out.append(cur)
            cur = None

        if dealt > 0.0:
            if cur is None:
                cur = Combo(i, now)
                cur.first_hit_t = now
                # Reach back over the approach: the decisions that STARTED the
                # combo are the ones worth learning, and they precede the hit.
                for j, ts in reversed(times):
                    if now - ts > lead_s:
                        break
                    cur.start_i, cur.start_t = j, ts
            cur.dealt += dealt
            cur.hits += 1
            cur.last_hit_t = now
            cur.end_i, cur.end_t = i, now

        if cur is not None:
            cur.taken += taken
            from .dataset import resolve
            me, en, _, _ = resolve(tick)
            if me and (not cur.attackers or cur.attackers[-1] != me):
                cur.attackers.append(me)
            if en and (not cur.defenders or cur.defenders[-1] != en):
                cur.defenders.append(en)
            act = (chars.get(me) or {}).get("cmd") if me else None
            if act and act != "None" and (not cur.actions or cur.actions[-1] != act):
                cur.actions.append(act)
            cur.tags += bool(tick.get("tag"))
            cur.escapes += bool(tick.get("escape"))
            cur.resources += bool(tick.get("resource"))

    if cur is not None:
        out.append(cur)
    return out


# ---------------- scoring and selection ----------------

def select(combos, min_damage=0.05, min_hits=2, require_net=True,
           top_frac=0.5, per_char_cap=None):
    """The combos worth cloning.

    Ranked WITHIN each character rather than globally. Damage is not comparable
    across the roster - a grappler's single throw outweighs three pokes - so a
    global cut-off would quietly train the corpus down to whichever handful of
    characters hit hardest, which is the opposite of covering the roster.

    `require_net` drops combos that cost more health than they won. A trade the
    bot lost is not an example, however much damage it happens to contain.
    """
    by_char = collections.defaultdict(list)
    for c in combos:
        if c.hits < min_hits or c.dealt < min_damage:
            continue
        if require_net and c.net <= 0:
            continue
        by_char[c.character].append(c)

    kept = []
    for char, cs in by_char.items():
        cs.sort(key=lambda c: c.dealt, reverse=True)
        n = max(1, int(round(len(cs) * top_frac)))
        if per_char_cap:
            n = min(n, per_char_cap)
        kept.extend(cs[:n])
    kept.sort(key=lambda c: c.start_i)
    return kept


def keep_indices(combos):
    """Tick indices covered by these combos, set-up included."""
    keep = set()
    for c in combos:
        keep.update(range(c.start_i, c.end_i + 1))
    return keep


def coverage(combos):
    """Per character: how many combos, how much damage, and the best one.

    This is the roster-coverage view - it answers which characters have enough
    successful examples to learn from and which are still thin.
    """
    out = {}
    for c in combos:
        s = out.setdefault(short(c.character), {
            "combos": 0, "hits": 0, "dealt": 0.0, "best": 0.0, "tagged": 0})
        s["combos"] += 1
        s["hits"] += c.hits
        s["dealt"] += c.dealt
        s["best"] = max(s["best"], c.dealt)
        s["tagged"] += bool(c.tags)
    return out


def repertoire(combos, min_count=2, max_len=6):
    """What reliably works, per character.

    Groups combos by the command sequence that produced them, so a sequence
    that keeps reappearing with real damage behind it is a combo the bot has
    actually found - as opposed to one lucky string it hit once.
    """
    groups = collections.defaultdict(list)
    for c in combos:
        name = short(c.character)
        seq = c.sequence[:max_len]
        # The unnamed bucket is several different fighters at once, so a
        # sequence "recurring" inside it proves nothing about any of them.
        if seq and name != UNNAMED:
            groups[(name, seq)].append(c)

    out = []
    for (char, seq), cs in groups.items():
        if len(cs) < min_count:
            continue
        dmg = [c.dealt for c in cs]
        out.append({
            "character": char,
            "sequence": seq,
            "count": len(cs),
            "mean_damage": sum(dmg) / len(dmg),
            "best_damage": max(dmg),
            "mean_hits": sum(c.hits for c in cs) / len(cs),
            "tagged": sum(1 for c in cs if c.tags),
        })
    out.sort(key=lambda r: (r["character"], -r["mean_damage"]))
    return out


def summarize(combos):
    """One-line totals for a set of combos."""
    if not combos:
        return "no combos"
    dealt = sum(c.dealt for c in combos)
    hits = sum(c.hits for c in combos)
    chars = {short(c.character) for c in combos}
    return (f"{len(combos)} combos, {hits} hits, {dealt:.2f} damage, "
            f"{len(chars)} characters")


def blind_fraction(ticks):
    """Share of ticks with no identified opponent.

    A recording where the `enemy` pointer is missing still produces training
    samples - featurize just fills the whole enemy half with zeros - so the
    model learns to attack from a blank picture of the opponent. Two runs on
    disk sit at 95% and 100%, which is 16k ticks of exactly that. Worth
    rejecting a file over rather than filtering tick by tick, because a run
    that blind was misconfigured, not unlucky.
    """
    n = blind = 0
    for t in ticks:
        if t.get("summary"):
            continue
        n += 1
        if not t.get("enemy"):
            blind += 1
    return blind / n if n else 1.0


class Picker:
    """A selection policy, and the cache identity that goes with it.

    Filtering happens at training time rather than by writing a curated copy of
    the corpus: the thresholds are a judgement call that will want retuning,
    and re-deriving them from the recordings costs nothing next to re-playing
    matches. `tag` folds the settings into the feature-cache key so two
    different policies cannot share an entry.
    """

    def __init__(self, min_damage=0.05, min_hits=2, top_frac=0.5,
                 require_net=True, per_char_cap=None,
                 break_s=BREAK_S, lead_s=LEAD_S):
        self.min_damage = min_damage
        self.min_hits = min_hits
        self.top_frac = top_frac
        self.require_net = require_net
        self.per_char_cap = per_char_cap
        self.break_s = break_s
        self.lead_s = lead_s

    @property
    def tag(self):
        return (f"pick(d{self.min_damage:g},h{self.min_hits},t{self.top_frac:g},"
                f"n{int(self.require_net)},c{self.per_char_cap or 0},"
                f"b{self.break_s:g},l{self.lead_s:g})")

    def combos(self, ticks):
        found = find(ticks, break_s=self.break_s, lead_s=self.lead_s)
        return select(found, min_damage=self.min_damage, min_hits=self.min_hits,
                      require_net=self.require_net, top_frac=self.top_frac,
                      per_char_cap=self.per_char_cap)

    def keep(self, ticks):
        return keep_indices(self.combos(ticks))

    def __repr__(self):
        return f"<Picker {self.tag}>"
