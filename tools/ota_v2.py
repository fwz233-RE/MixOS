"""Strict host implementation of protocol/OTA_V2.md (standard library only).

A timeout is an unknown outcome, never permission to start another transfer.
Only BEGIN writes application data; recovery uses the existing transaction.
"""
from contextlib import contextmanager
from dataclasses import dataclass
from enum import IntEnum
import hashlib
import re
import secrets
import struct
import time
import uuid

from ota_esp import Timeout, UpdateError
from protocol import Channel as C, Type as T

FORMAT = 2
REQUIRED_FEATURES = 0x17  # journal, exact hash, explicit reboot, maintenance health ACK
HEALTH_ACK_FEATURE = 0x10
HEALTH_CHALLENGE = 0x08
HEALTH_ACKED = 0x10
VALID = 2
EXACT_HASH = 2
JOURNAL = 1
BASELINE_PROTECTED = 4
ZERO_TX = bytes(16)

# The first deployed HEALTH_ACK receiver has a transcribed C byte-array error
# in its RELEASE_BASELINE guard. Keep the real baseline identity unchanged;
# only this exact independently measured receiver uses the legacy wire bytes.
LEGACY_GUARD_RECEIVER = (
    '7beaccdac481b4644526030e0cd9e561b119f940516ee09de739ca55c80c9414',
    '155c46f4e3d4e952657c1a8277be516920e8e3871f003de79bb1b7774c73aa49',
    910448,
)
LEGACY_GUARD_SHA = bytes.fromhex('7875d9a513acb95463b72e785eb160c70d03f85e965c3a30a93d954bb4cff55f')
CANONICAL_BASELINE_SHA = bytes.fromhex('7875d9a513acb95463b72e785ebd160c70d03f85e965c3a30a93d954bb4cff5f')


def baseline_guard_sha(baseline, replacement, proof):
    """Return a wire compatibility value, never an alternative image identity.

    The caller must already have explicit --allow-replace-baseline permission.
    No REFUSED/timeout fallback exists: choose before the single release request
    from fresh exact-file, ELF, VALID and acknowledged B measurement only.
    """
    canonical = bytes.fromhex(baseline['image_sha256'])
    known = (replacement['image_sha256'], replacement['elf_sha256'], replacement['image_bytes'])
    if known != LEGACY_GUARD_RECEIVER:
        return canonical, 'canonical'
    binding = proof.get('binding', {})
    if (canonical != CANONICAL_BASELINE_SHA or baseline.get('image_bytes') != 894560
            or baseline.get('elf_sha256') != 'cfacb3fe25931e918b8f46d1f840da8d57ba39ef799bd0af4629939b9185761a'
            or baseline.get('slot') != 'ota_0' or replacement.get('slot') != 'ota_1'
            or proof.get('actual_file_verified') is not True
            or proof.get('maintenance_health_acknowledged') is not True
            or proof.get('stored_sha256') != known[0] or proof.get('elf_sha256') != known[1]
            or binding.get('sha256') != known[0] or binding.get('size') != known[2]
            or binding.get('target') != 1 or proof.get('running_slot') != 1
            or proof.get('boot_slot') != 1 or proof.get('image_state') != VALID
            or proof.get('error') != 0 or proof.get('result') != 'ok'
            or proof.get('evidence_kind') != 'running-measurement'
            or type(proof.get('boot_id')) is not int or proof['boot_id'] <= 0
            or proof.get('flags', 0) & (EXACT_HASH | HEALTH_ACKED) != (EXACT_HASH | HEALTH_ACKED)):
        raise OutcomeError('legacy guard compatibility lacks exact healthy known-B proof', 'refused')
    return LEGACY_GUARD_SHA, 'known-health-ack-receiver-byte-array-fix'


class Op(IntEnum):
    BEGIN = 1
    END = 2
    QUERY = 3
    REBOOT = 4
    ABORT = 5
    VERIFY_RUNNING = 6
    RELEASE_BASELINE = 7
    HEALTH_ACK = 8


class Phase(IntEnum):
    IDLE = 0
    RECEIVING = 1
    HASH_CHECK = 2
    IMAGE_VALIDATE = 3
    SELECT_INTENT = 4
    BOOT_SELECTED = 5
    REBOOT_REQUESTED = 6
    RUNNING_PENDING_VERIFY = 7
    CONFIRMED = 8
    FAILED = 9
    ABORTED = 10
    VERIFYING_RUNNING = 11


class Result(IntEnum):
    OK = 0
    BUSY = 1
    CONFLICT = 2
    REFUSED = 3
    NOT_FOUND = 4
    ERROR = 5


class OutcomeError(UpdateError):
    def __init__(self, message, state='unknown', response=None, diagnostics=None):
        super().__init__(message)
        self.state = state
        self.response = response
        self.diagnostics = diagnostics


@dataclass(frozen=True)
class Binding:
    transaction: bytes
    sha256: bytes
    size: int
    target: int

    def __post_init__(self):
        if (len(self.transaction) != 16 or len(self.sha256) != 32 or
                not 0 <= self.size <= 0xFFFFFFFF or self.target not in (0, 1, 255)):
            raise ValueError('invalid transaction binding')

    def record(self):
        return dict(transaction=self.transaction.hex(), sha256=self.sha256.hex(),
                    size=self.size, target=self.target)

    @classmethod
    def from_record(cls, value):
        return cls(bytes.fromhex(value['transaction']), bytes.fromhex(value['sha256']),
                   value['size'], value['target'])


EMPTY = Binding(ZERO_TX, bytes(32), 0, 255)


def request_payload(op, request_id, binding=EMPTY, boot_id=0, *, health_challenge=0):
    if not request_id or not 0 <= boot_id <= 0xFFFFFFFF:
        raise ValueError('request_id must be nonzero; boot_id must be u32')
    if (type(health_challenge) is not int or not 0 <= health_challenge <= 0xFFFFFFFF
            or (op == Op.HEALTH_ACK) != (health_challenge != 0)):
        raise ValueError('only HEALTH_ACK requires a nonzero u32 challenge')
    return struct.pack('<BBHI16s32sIIB3xI', FORMAT, op, 0, request_id,
                       binding.transaction, binding.sha256, binding.size,
                       boot_id, binding.target, health_challenge)


@dataclass(frozen=True)
class Capabilities:
    features: int
    chunk: int
    window: int
    boot_id: int
    running: int
    boot: int
    state: int
    slots: tuple
    protected: int

    @classmethod
    def decode(cls, data):
        if len(data) != 36 or data[0] != FORMAT or data[6:8] != b'\0\0' or data[15]:
            raise OutcomeError('invalid v2 capabilities', 'refused')
        chunk, window = struct.unpack_from('<HH', data, 2)
        boot_id = struct.unpack_from('<I', data, 8)[0]
        a0, s0, a1, s1, protected = struct.unpack_from('<IIIII', data, 16)
        if not 1 <= chunk <= 508 or not 1 <= window <= 8 or not boot_id:
            raise OutcomeError('invalid capability limits or boot_id', 'refused')
        return cls(data[1], chunk, window, boot_id, data[12], data[13], data[14],
                   ((a0, s0), (a1, s1)), protected)

    def require(self, layout=None):
        if (self.features & REQUIRED_FEATURES != REQUIRED_FEATURES or
                (self.protected and not self.features & 8)):
            raise OutcomeError('receiver lacks required durable v2 capabilities; '
                               'use the separately approved bootstrap procedure', 'refused')
        if self.running not in (0, 1) or self.boot not in (0, 1):
            raise OutcomeError('unknown running/boot slot', 'unknown')
        if layout is not None:
            expected = tuple((layout[f'ota_{n}']['address'], layout[f'ota_{n}']['size'])
                             for n in (0, 1))
            if self.slots != expected:
                raise OutcomeError('live A/B layout differs from release manifest', 'refused')

    def record(self):
        return dict(features=self.features, boot_id=self.boot_id, running_slot=self.running,
                    boot_slot=self.boot, image_state=self.state, protected_slots=self.protected,
                    slots=[dict(address=a, size=s) for a, s in self.slots])


@dataclass(frozen=True)
class Response:
    op: int
    phase: Phase
    result: Result
    request_id: int
    binding: Binding
    received: int
    boot_id: int
    running: int
    boot: int
    state: int
    error: int
    flags: int
    stored_sha256: bytes
    elf_sha256: bytes
    message: str

    @classmethod
    def decode(cls, data):
        if len(data) != 192 or data[0] != FORMAT:
            raise OutcomeError('invalid v2 response size or version')
        try:
            phase, result = Phase(data[2]), Result(data[3])
            request_id = struct.unpack_from('<I', data, 4)[0]
            total, received, boot_id = struct.unpack_from('<III', data, 56)
            error, flags = struct.unpack_from('<iI', data, 72)
            binding = Binding(bytes(data[8:24]), bytes(data[24:56]), total, data[68])
            if not request_id or received > total:
                raise ValueError('invalid response counters')
            return cls(data[1], phase, result, request_id, binding, received, boot_id,
                       data[69], data[70], data[71], error, flags, bytes(data[80:112]),
                       bytes(data[112:144]), data[144:192].split(b'\0', 1)[0].decode('utf-8', 'replace'))
        except ValueError as exc:
            raise OutcomeError(f'invalid v2 response: {exc}') from exc

    def record(self):
        return dict(phase=self.phase.name.lower(), result=self.result.name.lower(),
                    binding=self.binding.record(), received=self.received, boot_id=self.boot_id,
                    running_slot=self.running, boot_slot=self.boot, image_state=self.state,
                    error=self.error, flags=self.flags, stored_sha256=self.stored_sha256.hex(),
                    elf_sha256=self.elf_sha256.hex(), message=self.message)


class Client:
    def __init__(self, link, timeout=30.0, sleep=time.sleep):
        self.link = link
        self.timeout = timeout
        self.sleep = sleep
        self.request_id = secrets.randbelow(0xFFFFFFFE) + 1

    @contextmanager
    def _request_diagnostics(self, operation, request_id, epoch, session):
        """Attach a bounded scalar snapshot; never retain frames or payloads.

        Link receive totals include frames read during a blocked send. The
        Client counters describe only frames examined by this request's wait.
        Heartbeat counts describe the Link's bounded, current-epoch history.
        """
        started = self.link.clock()
        received_before = getattr(self.link, 'maintenance_frames_received', None)
        diagnostics = dict(operation=operation, request_id=request_id,
                           expected_epoch=epoch, expected_session=session,
                           started_monotonic=started, timeout_seconds=self.timeout, stage='send',
                           maintenance_frames_examined=0, response_op_mismatches=0,
                           response_id_mismatches=0, response_size_mismatches=0,
                           decoder_errors_before=getattr(getattr(self.link, 'decoder', None), 'errors', None))
        try:
            yield diagnostics
        except (UpdateError, OSError) as exc:
            now = self.link.clock()
            beats = getattr(self.link, 'heartbeat_times', ())
            received_after = getattr(self.link, 'maintenance_frames_received', None)
            diagnostics.update(
                current_epoch=self.link.epoch, current_session=self.link.session,
                elapsed_seconds=max(0, now - started),
                valid_maintenance_frames_received=(received_after - received_before
                    if received_before is not None and received_after is not None else None),
                decoder_errors_after=getattr(getattr(self.link, 'decoder', None), 'errors', None),
                heartbeat_count=len(beats),
                heartbeat_count_since_start=sum(stamp >= started for stamp in beats),
                latest_heartbeat_age_seconds=max(0, now - beats[-1]) if beats else None)
            if isinstance(exc, Timeout):
                # Timeout text is host-generated, not a device error payload.
                diagnostics['waiting_reason'] = str(exc)[:512]
            exc.diagnostics = diagnostics
            raise  # Keep the original type, message, traceback and cause.

    def _wait(self, predicate, timeout, what, epoch, session, diagnostics, *, absolute_deadline=None):
        deadline = self.link.clock() + timeout
        if absolute_deadline is not None:
            deadline = min(deadline, absolute_deadline)
        while self.link.clock() < deadline:
            frames = self.link.poll()
            if self.link.clock() >= deadline:
                break  # A response processed after the budget is not timely evidence.
            if self.link.epoch != epoch or self.link.session != session:
                raise Timeout(f'link changed while waiting for {what}; outcome unknown')
            for frame in frames:
                if frame.epoch != epoch or frame.session != session or frame.channel != C.MAINTENANCE:
                    continue
                diagnostics['maintenance_frames_examined'] += 1
                if frame.type == T.ERROR:
                    raise OutcomeError('device error: ' + frame.payload.decode('utf-8', 'replace'))
                value = predicate(frame)
                if value is not None:
                    return value
            self.sleep(min(0.002, max(0, deadline - self.link.clock())))
        raise Timeout(f'timed out waiting for {what}; outcome unknown')

    def _send(self, kind, payload=b'', *, absolute_deadline=None):
        """Send and response wait share one finite per-request budget."""
        deadline = self.link.clock() + self.timeout
        if absolute_deadline is not None:
            deadline = min(deadline, absolute_deadline)
        remaining = deadline - self.link.clock()
        if remaining <= 0:
            raise Timeout('request deadline expired before send; outcome unknown')
        old = self.link.write_timeout
        self.link.write_timeout = min(old, remaining)
        try:
            self.link.send(C.MAINTENANCE, kind, self.link.session, payload)
        finally:
            self.link.write_timeout = old
        return max(0, deadline - self.link.clock())

    def capabilities(self, *, deadline=None):
        epoch, session = self.link.epoch, self.link.session
        try:
            with self._request_diagnostics('CAPS_QUERY', None, epoch, session) as diagnostics:
                cutoff = diagnostics['started_monotonic'] + self.timeout
                deadline = cutoff if deadline is None else min(deadline, cutoff)
                remaining = self._send(T.CAPS_QUERY, absolute_deadline=deadline)
                diagnostics['stage'] = 'wait'
                return self._wait(lambda f: Capabilities.decode(f.payload) if f.type == T.CAPS else None,
                                  remaining, 'v2 capabilities', epoch, session, diagnostics,
                                  absolute_deadline=deadline)
        except Timeout as exc:
            raise OutcomeError('no v2 capabilities established; writing is refused. '
                               'Silence does not prove legacy firmware. ' + str(exc), 'refused',
                               diagnostics=exc.diagnostics) from exc

    def call(self, op, binding=EMPTY, boot_id=0, check_binding=True, *, health_challenge=0, deadline=None):
        self.request_id = (self.request_id + 1) & 0xFFFFFFFF or 1
        request_id = self.request_id
        epoch, session = self.link.epoch, self.link.session
        with self._request_diagnostics(op.name, request_id, epoch, session) as diagnostics:
            cutoff = diagnostics['started_monotonic'] + self.timeout
            deadline = cutoff if deadline is None else min(deadline, cutoff)
            remaining = self._send(T.OTA_REQUEST, request_payload(op, request_id, binding, boot_id,
                                                                 health_challenge=health_challenge),
                                   absolute_deadline=deadline)
            diagnostics['stage'] = 'wait'

            def answer(frame):
                if frame.type != T.OTA_RESPONSE:
                    return None
                # Count ignored responses without retaining their payloads or
                # interpreting stale operations as the current request.
                if len(frame.payload) != 192:
                    diagnostics['response_size_mismatches'] += 1
                    if op == Op.VERIFY_RUNNING:
                        raise OutcomeError('invalid running measurement response size')
                    return None
                wrong_op = frame.payload[1] != op
                wrong_id = struct.unpack_from('<I', frame.payload, 4)[0] != request_id
                diagnostics['response_op_mismatches'] += int(wrong_op)
                diagnostics['response_id_mismatches'] += int(wrong_id)
                if wrong_op or wrong_id:
                    return None
                response = Response.decode(frame.payload)
                if response.result not in (Result.OK, Result.BUSY):
                    state = ('mismatch' if op == Op.VERIFY_RUNNING and response.result == Result.ERROR else
                             'refused' if response.result in (Result.REFUSED, Result.CONFLICT) else 'unknown')
                    raise OutcomeError(f'{op.name}: {response.result.name}: {response.message}', state, response)
                if (check_binding and binding.transaction != ZERO_TX and response.binding != binding and
                        not (op == Op.VERIFY_RUNNING and response.result == Result.BUSY)):
                    raise OutcomeError('response does not bind the complete transaction/file/slot', 'unknown', response)
                return response

            return self._wait(answer, remaining, op.name, epoch, session, diagnostics,
                              absolute_deadline=deadline)

    def acknowledge_health(self, binding, expected_elf, boot_id, challenge, *, deadline=None):
        # ACK carries the checked ELF instead of the file SHA. Its reply must
        # return the original measured file binding, not echo that request.
        request_binding = Binding(binding.transaction, expected_elf, binding.size, binding.target)
        response = self.call(Op.HEALTH_ACK, request_binding, boot_id, check_binding=False,
                             health_challenge=challenge, deadline=deadline)
        if response.result != Result.OK or response.binding != binding:
            raise OutcomeError('health ACK did not accept the exact measured file binding', 'mismatch', response)
        return response

    def query(self, binding=EMPTY, boot_id=0):
        return self.call(Op.QUERY, binding, boot_id)

    def stream(self, binding, image, caps, start=0, progress=None, stall=2.0):
        """Bound every ACK by bytes actually sent. Link resets never resume data."""
        epoch, session = self.link.epoch, self.link.session
        acked = sent = start
        quiet = self.link.clock()
        stalled = 0
        while acked < len(image):
            while sent < len(image) and sent - acked < caps.chunk * caps.window:
                end = min(sent + caps.chunk, len(image))
                self.link.send(C.MAINTENANCE, T.OTA_DATA, session,
                               struct.pack('<I', sent) + image[sent:end])
                sent = end
            frames = self.link.poll()
            if self.link.epoch != epoch or self.link.session != session:
                raise OutcomeError('link interrupted receive; no cross-link resume or blind reflash')
            moved = False
            for frame in frames:
                if frame.channel != C.MAINTENANCE or frame.session != session or frame.epoch != epoch:
                    continue
                if frame.type == T.ERROR:
                    raise OutcomeError('transfer error: ' + frame.payload.decode('utf-8', 'replace'))
                if frame.type != T.OTA_ACK or len(frame.payload) != 4:
                    continue
                offset = struct.unpack('<I', frame.payload)[0]
                if offset > sent:
                    raise OutcomeError(f'ACK {offset} exceeds sent bytes {sent}')
                if offset > acked:
                    acked, moved = offset, True
            if moved:
                stalled = 0
                quiet = self.link.clock()
                if progress:
                    progress(acked, len(image))
            elif self.link.clock() - quiet >= min(stall, self.timeout):
                response = self.query(binding, caps.boot_id)
                if response.phase != Phase.RECEIVING or response.boot_id != caps.boot_id:
                    raise OutcomeError('receive state changed; automatic restart is refused', response=response)
                if not acked <= response.received <= sent:
                    raise OutcomeError('QUERY acknowledged bytes outside sent range', response=response)
                stalled = stalled + 1 if response.received == acked else 0
                if stalled > 4:
                    raise OutcomeError('receive stalled; outcome requires inspection')
                sent = acked = response.received
                quiet = self.link.clock()
            self.sleep(0.002)
        return acked


def check_response(response):
    if response.phase in (Phase.FAILED, Phase.ABORTED):
        raise OutcomeError(f'transaction {response.phase.name.lower()}: {response.message}',
                           'failed', response)


def verify_actual(client, binding, expected_elf, deadline, heartbeat_seconds=6.0,
                  heartbeat_gap=3.5, *, no_journal=False):
    """Verify fresh measurements, with at most two extra read-only requests.

    Only a fully sent VERIFY_RUNNING that timed out on the unchanged, actively
    heartbeating link may be repeated. CAPS and HEALTH_ACK are never retried.
    The absolute caller deadline covers every request and is never extended.
    Diagnostics retain counters, the last attempt and at most three timeouts;
    neither wire payloads nor an unbounded measurement history are retained.
    """
    recovery = dict(measurement_attempts=0, extra_requests=0, timeout_events=[],
                    request_limit_seconds=5.0, extra_request_limit=2,
                    deadline_monotonic=deadline, recovered=False)
    try:
        result = _verify_actual(client, binding, expected_elf, deadline,
                                heartbeat_seconds, heartbeat_gap, no_journal, recovery)
    except (UpdateError, OSError) as exc:
        recovery['outcome'] = 'failed'
        diagnostics = getattr(exc, 'diagnostics', None)
        if not isinstance(diagnostics, dict):
            diagnostics = {}
            exc.diagnostics = diagnostics
        # Preserve the original request diagnostics object and exception chain.
        diagnostics['verification'] = recovery
        raise
    recovery['outcome'] = 'confirmed'
    recovery['recovered'] = recovery['extra_requests'] > 0
    result['diagnostics'] = dict(verification=recovery)
    return result


def _verify_actual(client, binding, expected_elf, deadline, heartbeat_seconds,
                   heartbeat_gap, no_journal, recovery):
    """Measure an explicitly known file, not an inferred transaction history.

    In no_journal mode QUERY is never bound to the synthetic measurement ID:
    firmware's empty journal stays IDLE. Repeated opcode 6 responses establish
    the actual bytes, identity and VALID state on one boot. BUSY snapshots may
    describe an unrelated journal and are not measurement evidence.
    """
    def bounded(operation, request_limit=None):
        remaining = deadline - client.link.clock()
        if remaining <= 0:
            raise OutcomeError('running verification deadline expired', 'unknown')
        old = client.timeout
        client.timeout = min(old, remaining) if request_limit is None else min(old, remaining, request_limit)
        try:
            return operation()
        finally:
            client.timeout = old

    # Real transport sends/waits also receive the absolute cutoff; deterministic
    # simple test clients retain their timeout-only interface, never a retry bypass.
    deadline_args = dict(deadline=deadline) if isinstance(client, Client) else {}
    caps = bounded(lambda: client.capabilities(**deadline_args))
    caps.require()
    boot_id, epoch, session = caps.boot_id, client.link.epoch, client.link.session
    recovery.update(boot_id=boot_id, epoch=epoch, session=session)
    measurement = None
    valid_since = None
    acknowledged_challenge = None

    def retry_reason(exc):
        """Absence of evidence is a refusal, including older/fake diagnostics."""
        diag = getattr(exc, 'diagnostics', None)
        if not isinstance(diag, dict):
            return 'missing-request-diagnostics'
        if (diag.get('operation') != 'VERIFY_RUNNING' or diag.get('stage') != 'wait'
                or type(diag.get('request_id')) is not int or not diag['request_id']
                or diag['request_id'] != getattr(client, 'request_id', None)
                or isinstance(exc.__cause__, OSError)):
            return 'unsafe-request-timeout'
        if (not epoch or not session or client.link.epoch != epoch or client.link.session != session
                or diag.get('expected_epoch') != epoch or diag.get('current_epoch') != epoch
                or diag.get('expected_session') != session or diag.get('current_session') != session):
            return 'link-changed'
        if (diag.get('response_size_mismatches') != 0
                or type(diag.get('decoder_errors_before')) is not int
                or diag.get('decoder_errors_after') != diag['decoder_errors_before']):
            return 'malformed-or-unobserved-input'
        count, age = diag.get('heartbeat_count_since_start'), diag.get('latest_heartbeat_age_seconds')
        if (type(count) is not int or count < 1 or type(age) not in (int, float)
                or not 0 <= age <= heartbeat_gap):
            return 'no-fresh-heartbeat'
        if client.link.clock() >= deadline:
            return 'deadline-exhausted'
        if recovery['extra_requests'] >= recovery['extra_request_limit']:
            return 'retry-budget-exhausted'
        return None

    def measure():
        retry_event = None
        while True:
            # Check before counting or issuing an attempt, including retries.
            if client.link.clock() >= deadline:
                raise OutcomeError('running verification deadline expired', 'unknown', measurement)
            attempt = dict(outcome='requesting')
            previous_event = retry_event

            def request():
                # Count only calls actually issued after bounded() checks time.
                recovery['measurement_attempts'] += 1
                if previous_event is not None:
                    recovery['extra_requests'] += 1
                attempt.update(number=recovery['measurement_attempts'], timeout_seconds=client.timeout)
                recovery['last_attempt'] = attempt
                return client.call(Op.VERIFY_RUNNING, binding, boot_id, **deadline_args)

            try:
                response = bounded(request, recovery['request_limit_seconds'])
                attempt.update(request_id=response.request_id, outcome='response-received')
                return response
            except Timeout as exc:
                attempt.update(request_id=getattr(client, 'request_id', None), outcome='timeout')
                reason = retry_reason(exc)
                diag = getattr(exc, 'diagnostics', None)
                # Explicit whitelist: never copy payloads, arbitrary exception
                # messages or a nested verification report into the event list.
                fields = ('request_id', 'stage', 'timeout_seconds', 'elapsed_seconds',
                          'expected_epoch', 'current_epoch', 'expected_session', 'current_session',
                          'heartbeat_count_since_start', 'latest_heartbeat_age_seconds',
                          'response_id_mismatches', 'response_op_mismatches',
                          'response_size_mismatches', 'decoder_errors_before', 'decoder_errors_after')
                event = {key: diag.get(key) for key in fields} if isinstance(diag, dict) else {}
                event.update(attempt=attempt['number'], retry=reason is None, stop_reason=reason)
                recovery['timeout_events'].append(event)
                if reason is not None:
                    state = 'pending' if measurement is not None and measurement.state in (0, 1) else 'unknown'
                    raise OutcomeError('actual-running measurement timed out: ' + str(exc), state, measurement,
                                       diagnostics=diag) from exc
                retry_event = event
                # Client.call allocates a fresh request ID. Keep this boot,
                # complete binding, session and total deadline unchanged.
            except (UpdateError, OSError):
                attempt.update(request_id=getattr(client, 'request_id', None), outcome='failed')
                raise
            finally:
                if previous_event is not None:
                    previous_event.update(retry_request_id=attempt.get('request_id'),
                                          retry_outcome=attempt['outcome'])

    response = measure()
    while client.link.clock() < deadline:
        if client.link.epoch != epoch or client.link.session != session or response.boot_id != boot_id:
            raise OutcomeError('boot/link changed during running verification')
        busy = response.phase == Phase.VERIFYING_RUNNING or response.result == Result.BUSY
        if not busy:
            if response.result != Result.OK or response.error:
                raise OutcomeError('running measurement reports an error', 'mismatch', response)
            if response.binding != binding or response.received != binding.size:
                raise OutcomeError('measurement does not bind the exact requested file length', 'mismatch', response)
            if response.running != binding.target or response.boot != binding.target:
                raise OutcomeError('actual running/selected slot differs from target; cause unknown', 'mismatch', response)
            if response.elf_sha256 != expected_elf:
                raise OutcomeError('full running ELF identity mismatch', 'mismatch', response)
            required = EXACT_HASH if no_journal else JOURNAL | EXACT_HASH
            if response.flags & required != required or response.stored_sha256 != binding.sha256:
                raise OutcomeError('actual running file SHA256 mismatch or required journal unavailable', 'mismatch', response)
            measurement = response
            challenge_match = re.fullmatch(r'health-challenge:([0-9a-f]{8})', response.message)
            if not response.flags & HEALTH_CHALLENGE or challenge_match is None:
                raise OutcomeError('exact measurement lacks maintenance health challenge', 'refused', response)
            challenge = int(challenge_match[1], 16)
            if not challenge:
                raise OutcomeError('maintenance health challenge must be nonzero', 'refused', response)
            if acknowledged_challenge is None:
                response = bounded(lambda: client.acknowledge_health(binding, expected_elf, boot_id, challenge,
                                                                      **deadline_args))
                acknowledged_challenge = challenge
                continue  # Validate the ACK's identity/hash/boot/state, too.
            if challenge != acknowledged_challenge or not response.flags & HEALTH_ACKED:
                raise OutcomeError('maintenance health acknowledgement was lost or changed', 'unknown', response)
            valid_phase = response.phase == Phase.CONFIRMED or (no_journal and response.phase == Phase.IDLE)
            if response.state == VALID and valid_phase:
                if valid_since is None:
                    valid_since = client.link.clock()
                now = client.link.clock()
                beats = [x for x in client.link.heartbeat_times if x >= valid_since]
                # An old gap invalidates only the earlier window, not a later
                # sustained interval on this same verified boot/link.
                for index in range(len(beats) - 1, 0, -1):
                    if beats[index] - beats[index - 1] > heartbeat_gap:
                        beats = beats[index:]
                        break
                if (response.op == Op.VERIFY_RUNNING and len(beats) >= 3
                        and beats[-1] - beats[0] >= heartbeat_seconds and
                        now - beats[-1] <= heartbeat_gap and
                        all(b - a <= heartbeat_gap for a, b in zip(beats, beats[1:]))):
                    return dict(state='confirmed', **response.record(),
                                actual_file_verified=True, measurement=measurement.record(),
                                maintenance_health_acknowledged=True,
                                evidence_kind='running-measurement' if no_journal else 'transaction-and-measurement',
                                heartbeat_count=len(beats), heartbeat_seconds=beats[-1] - beats[0])
            elif response.state in (0, 1):
                valid_since = None
            else:
                raise OutcomeError('candidate is neither pending nor confirmed VALID', 'mismatch', response)
        client.sleep(min(0.1, max(0, deadline - client.link.clock())))
        if client.link.clock() >= deadline:
            break
        # QUERY is durable history, never proof that opcode 6 completed.
        # Cached opcode-6 measurements remain bound to this boot/file length.
        response = measure()
    state = 'pending' if measurement is not None and (response.state in (0, 1) or measurement.state in (0, 1)) else 'unknown'
    raise OutcomeError('running verification/VALID/sustained heartbeat deadline expired', state, response)


class Updater:
    """Single bounded transaction; connect() must reopen the same stable device.

    record(event) MUST durably save the binding before any BEGIN is transmitted.
    A saved binding resumes observation/END/REBOOT only, never a receive restart.
    """
    def __init__(self, connect, timeout=30.0, health_timeout=90.0,
                 heartbeat_seconds=6.0, clock=time.monotonic, sleep=time.sleep):
        self.connect = connect
        self.timeout = timeout
        self.health_timeout = health_timeout
        self.heartbeat_seconds = heartbeat_seconds
        self.clock = clock
        self.sleep = sleep
        self.client = None

    def open(self):
        self.close()
        link = self.connect()
        self.client = Client(link, self.timeout, self.sleep)
        return self.client

    def close(self):
        if self.client is not None:
            client, self.client = self.client, None
            client.link.transport.close()

    def observe(self, binding, deadline):
        """Recover lost END/REBOOT replies by QUERY, without waiting for absence."""
        last = None
        while self.clock() < deadline:
            try:
                if self.client is None:
                    self.open()
                return self.client.query(binding)
            except (OSError, Timeout) as exc:
                last = exc
                self.close()
                self.sleep(0.1)
        raise OutcomeError(f'cannot establish transaction outcome: {last}')

    def run(self, image, manifest, record, saved=None, allow_replace_baseline=False):
        expected = bytes.fromhex(manifest['app']['elf_sha256'])
        digest = hashlib.sha256(image).digest()
        client = self.open()
        caps = client.capabilities()
        caps.require(manifest['layout'])
        latest = client.query()
        if saved:
            binding = Binding.from_record(saved['binding'])
            source_boot = saved['source_boot_id']
            if binding.sha256 != digest or binding.size != len(image):
                raise OutcomeError('saved transaction does not match this release', 'refused')
            response = client.query(binding)
            if response.phase == Phase.RECEIVING:
                raise OutcomeError('interrupted receive cannot be resumed; inspect/abort explicitly')
        else:
            # Even a CONFIRMED journal must be independently checked, not reflashed.
            if (latest.binding.sha256 == digest and latest.binding.size == len(image) and
                    latest.binding.target == caps.running and latest.phase in
                    (Phase.CONFIRMED, Phase.RUNNING_PENDING_VERIFY, Phase.REBOOT_REQUESTED)):
                binding = latest.binding
                return verify_actual(client, binding, expected, self.clock() + self.health_timeout,
                                     self.heartbeat_seconds)
            if (caps.state != VALID or caps.boot != caps.running or latest.phase not in
                    (Phase.IDLE, Phase.CONFIRMED, Phase.FAILED, Phase.ABORTED)):
                raise OutcomeError('unresolved prior transaction or non-VALID running app; inspect before a new apply', 'refused')
            target = 1 - caps.running
            if len(image) > caps.slots[target][1]:
                raise OutcomeError('image does not fit inactive slot', 'refused')
            if caps.protected & (1 << target):
                if target != 0 or not allow_replace_baseline:
                    raise OutcomeError('ota_0 recovery baseline is protected; explicit --allow-replace-baseline is required', 'refused')
                baseline = manifest.get('protected_baseline')
                if not baseline:
                    raise OutcomeError('release manifest lacks full protected baseline binding', 'refused')
                # An explicit known-package binding is mandatory even when an
                # old journal exists. Never authorize erasing recovery by using
                # whatever ELF/hash an unknown fallback happens to report.
                replacement = manifest.get('verified_replacement')
                if not replacement or replacement.get('slot') != 'ota_1':
                    raise OutcomeError('release requires an explicit known verified_replacement package binding', 'refused')
                known = Binding(uuid.uuid4().bytes, bytes.fromhex(replacement['image_sha256']),
                                replacement['image_bytes'], 1)
                proof = verify_actual(client, known, bytes.fromhex(replacement['elf_sha256']),
                                      self.clock() + self.health_timeout, self.heartbeat_seconds,
                                      no_journal=True)
                record(dict(event='replacement-measured', replacement=replacement, evidence=proof))
                guard_sha, guard_mode = baseline_guard_sha(baseline, replacement, proof)
                guard = Binding(uuid.uuid4().bytes, guard_sha, baseline['image_bytes'], 0)
                record(dict(event='release-baseline-requested', baseline=baseline,
                            wire_guard_sha256=guard_sha.hex(), wire_guard_mode=guard_mode))
                client.call(Op.RELEASE_BASELINE, guard, caps.boot_id, check_binding=False)
                caps = client.capabilities()
                if caps.protected & 1:
                    raise OutcomeError('baseline guard release was not confirmed; no flash written')
            binding = Binding(uuid.uuid4().bytes, digest, len(image), target)
            source_boot = caps.boot_id
            record(dict(event='transaction-bound', binding=binding.record(), source_boot_id=source_boot))
            begin_epoch = client.link.epoch
            try:
                response = client.call(Op.BEGIN, binding, source_boot)
            except Timeout:
                if client.link.epoch != begin_epoch:
                    raise OutcomeError('BEGIN link interrupted; no receive resume')
                # Same binding, boot, and session, but fresh wire sequence/request ID.
                response = client.call(Op.BEGIN, binding, source_boot)
            deadline = self.clock() + self.timeout
            while response.result == Result.BUSY and response.phase == Phase.RECEIVING and self.clock() < deadline:
                self.sleep(0.05)
                response = client.query(binding, source_boot)
            check_response(response)
            if response.phase != Phase.RECEIVING or response.received != 0 or response.boot_id != source_boot:
                raise OutcomeError('BEGIN did not establish a fresh receive session', response=response)
            client.stream(binding, image, caps)
            record(dict(event='end-requested'))
            try:
                response = client.call(Op.END, binding, source_boot)
            except (Timeout, OSError):
                response = self.observe(binding, self.clock() + self.timeout)
        deadline = self.clock() + self.timeout
        while response.phase not in (Phase.BOOT_SELECTED, Phase.REBOOT_REQUESTED,
                                     Phase.RUNNING_PENDING_VERIFY, Phase.CONFIRMED):
            check_response(response)
            if self.clock() >= deadline:
                raise OutcomeError('END completion unknown; QUERY the same transaction', response=response)
            if response.phase == Phase.RECEIVING and response.received == binding.size:
                # Lost END before acceptance; repeating the same END cannot reflash.
                try:
                    response = self.client.call(Op.END, binding, source_boot)
                except (OSError, Timeout):
                    response = self.observe(binding, deadline)
            else:
                if self.clock() >= deadline:
                    raise OutcomeError('END completion unknown; QUERY the same transaction', response=response)
                self.sleep(0.05)
                response = self.observe(binding, deadline)
        if response.phase in (Phase.BOOT_SELECTED, Phase.REBOOT_REQUESTED):
            if response.boot_id == source_boot and response.running != binding.target:
                record(dict(event='reboot-requested'))
                try:
                    self.client.call(Op.REBOOT, binding, source_boot)
                except (OSError, Timeout):
                    pass  # QUERY, not a presumed reboot or presumed failure.
        deadline = self.clock() + self.health_timeout
        while self.clock() < deadline:
            response = self.observe(binding, deadline)
            check_response(response)
            if response.running == binding.target and response.boot_id != source_boot:
                break
            if response.boot_id != source_boot and response.running != binding.target:
                raise OutcomeError('new boot runs a different slot; rollback is not established by silence', 'mismatch', response)
            # A duplicate REBOOT is safe only with the recorded source boot ID.
            if response.phase in (Phase.BOOT_SELECTED, Phase.REBOOT_REQUESTED) and response.boot_id == source_boot:
                try:
                    self.client.call(Op.REBOOT, binding, source_boot)
                except (OSError, Timeout):
                    self.close()
            self.sleep(0.1)
        else:
            raise OutcomeError('reboot/running slot not established by deadline')
        record(dict(event='verifying-running', response=response.record()))
        return verify_actual(self.client, binding, expected, deadline, self.heartbeat_seconds)
