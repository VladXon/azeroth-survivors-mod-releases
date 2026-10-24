"""Pure co-op choice-window timing and ownership policy.

This module has no game, process, networking, or patcher dependencies.  Times are
monotonic seconds.  Call :meth:`ChoiceShield.reset` when a run is replaced.
"""


class ChoiceWindow:
    """Track one player's picking windows and the chained shield recovery."""

    WINDOW_SECONDS = 4.0
    CHAIN_SECONDS = 8.0
    RECOVERY_SECONDS = 5.0

    def __init__(self):
        self.choosing = False
        self.shield = False
        self.started_at = None
        self.chain_started_at = None
        self.closed_at = None
        self._last_now = None
        self._serial = None
        self._window_serial = None

    def reset(self):
        self.__init__()

    def update(self, now, choosing, serial=None):
        """Update phase state and return whether this window grants a shield.

        ``serial`` distinguishes adjacent phase-2 picker windows when there was
        no playing frame between them. Repeated serials do not restart timers.
        """
        now = float(now)
        choosing = bool(choosing)
        # A monotonic clock must not move backwards. Ignore an out-of-order
        # observation rather than letting it extend a protection interval.
        if self._last_now is not None and now < self._last_now:
            return self.shield
        self._last_now = now
        changed_window = choosing and (
            not self.choosing or (serial is not None and serial != self._window_serial)
        )
        if changed_window:
            if self.chain_started_at is None or (
                self.closed_at is not None
                and now - self.closed_at >= self.RECOVERY_SECONDS
            ):
                self.chain_started_at = now
            self.started_at = now
            self._window_serial = serial
        if self.choosing and not choosing:
            self.closed_at = now
        self.choosing = choosing
        if serial is not None:
            self._serial = serial
        self.shield = bool(
            choosing
            and self.started_at is not None
            and self.chain_started_at is not None
            and now - self.started_at < self.WINDOW_SECONDS
            and now - self.chain_started_at < self.CHAIN_SECONDS
        )
        return self.shield


def _is_choosing(phase):
    return phase == 2 or str(phase).lower() in {"choosing", "choice", "pick"}


class ChoiceShield:
    """Pure damage policy for both co-op players.

    ``update`` returns a dictionary of current policy values. Incoming damage
    multipliers are 0 (immune), .1 (90% reduction), .8 (20% reduction), or 1.
    """

    INVULNERABLE_SECONDS = 5.0

    def __init__(self):
        self.mine = ChoiceWindow()
        self.partner = ChoiceWindow()
        self._mine_was_choosing = False
        self._mine_serial = None
        self._mine_overlapped = False
        self._grace_started_at = None
        self._partner_seen_at = None
        self._partner_phase = None

    def reset(self):
        self.__init__()

    def update(self, now, mine_phase, partner_phase, *, partner_live,
               enabled=True, mine_serial=None, partner_serial=None):
        now = float(now)
        mine_choosing = _is_choosing(mine_phase)
        partner_phase_known = partner_phase is not None
        partner_choosing = bool(partner_live and partner_phase_known
                                and _is_choosing(partner_phase))
        new_mine_window = mine_choosing and (
            not self._mine_was_choosing
            or (mine_serial is not None and mine_serial != self._mine_serial)
        )
        if new_mine_window:
            self._mine_overlapped = False
            self._grace_started_at = None
        own_active = self.mine.update(now, mine_choosing, mine_serial)

        # Partner updates are evidence only while the relay is live and carries
        # a phase. A repeated frame cannot extend a picker window; its serial
        # and the original opening time remain authoritative.
        if partner_live and partner_phase_known:
            self._partner_seen_at = now
            self._partner_phase = partner_phase
            partner_active = self.partner.update(
                now, partner_choosing, partner_serial
            )
        else:
            self.partner.update(now, False)
            partner_active = False
        partner_fresh = bool(
            partner_live and self._partner_seen_at is not None
            and 0 <= now - self._partner_seen_at <= 4.0
            and partner_phase_known
        )
        partner_active = partner_active and partner_fresh
        both_choosing = mine_choosing and partner_choosing
        both = both_choosing
        if both_choosing:
            self._mine_overlapped = True
        # The grace belongs to this player's window: the partner ending or
        # becoming stale must not start it while this player is still choosing.
        if self._mine_was_choosing and not mine_choosing:
            if self._mine_overlapped:
                self._grace_started_at = now
            self._mine_overlapped = False
        grace = bool(
            self._grace_started_at is not None
            and 0 <= now - self._grace_started_at < 1.0
        )
        self._mine_was_choosing = mine_choosing
        if mine_serial is not None:
            self._mine_serial = mine_serial

        if not enabled:
            own_active = partner_active = both = grace = False
        own_shield = bool(enabled and own_active)
        partner_shield = bool(enabled and partner_active)
        invulnerable = bool(
            enabled and mine_choosing and self.mine.started_at is not None
            and now < self.mine.started_at + self.INVULNERABLE_SECONDS
        )
        if invulnerable:
            incoming = 0.0
        elif grace:
            incoming = 0.1
        elif own_shield:
            incoming = 0.1
        elif partner_shield and not both:
            incoming = 0.8
        else:
            incoming = 1.0
        outgoing = 0.0 if (enabled and mine_choosing) else (
            1.5 if partner_shield and not both else 1.0
        )
        return {
            "incoming": incoming,
            "invulnerable": invulnerable,
            "outgoing": outgoing,
            "own_shield": own_shield,
            "partner_shield": partner_shield,
            "both": bool(both),
            "grace": bool(grace),
            "choosing": bool(mine_choosing),
            "partner_choosing": bool(partner_choosing),
        }


def choice_owner(prev, d_host, d_guest, host_shield, guest_shield,
                 both=False, boss=False):
    """Return host(0), guest(1), or hold(2) as target ownership.

    This only supplies the shield transfer policy. ``prev`` is the existing
    owner selected by normal co-op logic. ``None`` distance is treated as
    unavailable. A shielded owner transfers only to an ally within 50 yards;
    otherwise hold the enemy in place. Bosses and simultaneous picks retain
    their existing owner.
    """
    if both or boss:
        return prev
    if prev == 0 and host_shield:
        return 1 if d_guest is not None and d_guest <= 50 else 2
    if prev == 1 and guest_shield:
        return 0 if d_host is not None and d_host <= 50 else 2
    return prev
