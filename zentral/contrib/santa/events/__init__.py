from datetime import datetime
import logging
from zentral.conf import settings
from zentral.contrib.inventory.models import File
from zentral.contrib.santa.models import Bundle, EnrolledMachine, Target
from zentral.contrib.santa.utils import add_bundle_binary_targets, update_metabundles, update_or_create_targets
from zentral.core.events.base import BaseEvent, EventMetadata, EventRequest, register_event_type
from zentral.utils.certificates import APPLE_DEV_ID_ISSUER_CN, parse_apple_dev_id
from zentral.utils.text import shard


logger = logging.getLogger('zentral.contrib.santa.events')


ALL_EVENTS_SEARCH_DICT = {"tag": "santa"}


class SantaEnrollmentEvent(BaseEvent):
    event_type = "santa_enrollment"
    tags = ["santa"]

    def get_linked_objects_keys(self):
        keys = {}
        configuration = self.payload.get("configuration")
        if configuration:
            keys["santa_configuration"] = [(configuration.get("pk"),)]
        return keys


register_event_type(SantaEnrollmentEvent)


class SantaPreflightEvent(BaseEvent):
    event_type = "santa_preflight"
    tags = ["santa", "heartbeat"]

    @classmethod
    def get_machine_heartbeat_timeout(cls, serial_number):
        enrolled_machines = EnrolledMachine.objects.get_for_serial_number(serial_number)
        count = len(enrolled_machines)
        if not count:
            return
        if count > 1:
            logger.warning("Multiple enrolled machines found for %s", serial_number)
        timeout = 2 * enrolled_machines[0].enrollment.configuration.full_sync_interval
        logger.debug("Santa preflight event heartbeat timeout for machine %s: %s", serial_number, timeout)
        return timeout


register_event_type(SantaPreflightEvent)


class SantaEventEvent(BaseEvent):
    event_type = "santa_event"
    tags = ["santa"]
    payload_aggregations = [
        ("decision", {"type": "terms", "bucket_number": 10, "label": "Decisions"}),
        ("file_bundle_name", {"type": "terms", "bucket_number": 10, "label": "Bundle names"}),
        ("bundles", {"type": "table", "bucket_number": 100, "label": "Bundles",
                     "columns": [("file_bundle_name", "Name"),
                                 ("file_bundle_id", "ID"),
                                 ("file_bundle_path", "File path"),
                                 ("file_bundle_version_string", "Version str.")]}),
    ]

    def get_notification_context(self, probe):
        ctx = super().get_notification_context(probe)
        if 'decision' in self.payload:
            ctx['decision'] = self.payload['decision']
        if 'file_name' in self.payload:
            ctx['file_name'] = self.payload['file_name']
        if 'file_path' in self.payload:
            ctx['file_path'] = self.payload['file_path']
        return ctx

    def iter_signing_chain(self):
        signing_chain = self.payload.get("signing_chain")
        if isinstance(signing_chain, list):
            yield from signing_chain
            return
        for i in range(3):
            cert = self.payload.get(f"signing_cert_{i}")
            if isinstance(cert, dict):
                yield cert

    def get_linked_objects_keys(self):
        keys = {}
        file_args = []
        file_sha256 = self.payload.get("file_sha256")
        if file_sha256:
            file_args.append(("sha256", file_sha256))
        cdhash = self.payload.get("cdhash")
        if cdhash:
            file_args.append(("cdhash", cdhash))
        signing_id = self.payload.get("signing_id")
        if signing_id:
            file_args.append(("apple_signing_id", signing_id))
        if file_args:
            keys['file'] = file_args
        team_id = self.payload.get("team_id")
        cert_sha256_list = []
        signing_chain = list(self.iter_signing_chain())
        for cert_idx, cert in enumerate(signing_chain):
            # cert sha256
            cert_sha256 = cert.get("sha256")
            if cert_sha256:
                cert_sha256_list.append(("sha256", cert_sha256))
            # Apple Developer Team ID
            if not team_id and cert_idx == 0:
                try:
                    issuer_cn = signing_chain[cert_idx + 1]["cn"]
                except (IndexError, KeyError):
                    continue
                if issuer_cn != APPLE_DEV_ID_ISSUER_CN:
                    continue
                try:
                    _, team_id = parse_apple_dev_id(cert["cn"])
                except (KeyError, ValueError):
                    pass
        if team_id:
            keys["apple_team_id"] = [(team_id,)]
        if cert_sha256_list:
            keys['certificate'] = cert_sha256_list
        return keys


register_event_type(SantaEventEvent)


class SantaLogEvent(BaseEvent):
    event_type = "santa_log"
    tags = ["santa"]


register_event_type(SantaLogEvent)


class SantaRuleSetUpdateEvent(BaseEvent):
    event_type = "santa_ruleset_update"
    tags = ["santa"]

    def get_linked_objects_keys(self):
        keys = {}
        configurations = self.payload.get("configurations")
        if configurations:
            for configuration in configurations:
                keys.setdefault("santa_configuration", []).append((configuration.get("pk"),))
        ruleset = self.payload.get("ruleset")
        if ruleset:
            keys["santa_ruleset"] = [(ruleset.get("pk"),)]
        return keys


register_event_type(SantaRuleSetUpdateEvent)


class TargetEventMixin:
    def add_target_to_linked_objects_keys(self, keys, attr="target"):
        target = self.payload
        for payload_key in attr.split("."):
            target = target.get(payload_key)
            if not target:
                return
        target_type = target.get("type")
        if not target_type:
            return
        try:
            target_type = Target.Type(target_type)
        except (ValueError, TypeError):
            logger.error("Invalid target type")
            return
        if target_type == Target.Type.CDHASH:
            cdhash = target.get("cdhash")
            if cdhash:
                keys.setdefault("file", []).append(("cdhash", cdhash))
        elif target_type == Target.Type.SIGNING_ID:
            signing_id = target.get("signing_id")
            if signing_id:
                keys.setdefault("file", []).append(("apple_signing_id", signing_id))
        elif target_type == Target.Type.TEAM_ID:
            team_id = target.get("team_id")
            if team_id:
                keys.setdefault("apple_team_id", []).append((team_id,))
        else:
            sha256 = target.get("sha256")
            if sha256:
                if target_type == Target.Type.BINARY:
                    key_attr = "file"
                else:
                    key_attr = target_type.name.lower()
                keys.setdefault(key_attr, []).append(("sha256", sha256))


class SantaRuleUpdateEvent(TargetEventMixin, BaseEvent):
    event_type = "santa_rule_update"
    tags = ["santa"]

    def get_linked_objects_keys(self):
        keys = {}
        self.add_target_to_linked_objects_keys(keys, attr="rule.target")
        rule = self.payload.get("rule")
        if not rule:
            return keys
        configuration = rule.get("configuration")
        if configuration:
            keys["santa_configuration"] = [(configuration.get("pk"),)]
        ruleset = rule.get("ruleset")
        if ruleset:
            keys["santa_ruleset"] = [(ruleset.get("pk"),)]
        return keys


register_event_type(SantaRuleUpdateEvent)


class SantaBallotEvent(TargetEventMixin, BaseEvent):
    event_type = "santa_ballot"
    tags = ["santa"]

    def get_linked_objects_keys(self):
        keys = {}
        for attr in ("target", "event_target"):
            self.add_target_to_linked_objects_keys(keys, attr)
        realm_user = self.payload.get("realm_user")
        if realm_user:
            keys["realm_user"] = [(realm_user["pk"],)]
        for vote in self.payload.get("votes", []):
            keys["santa_configuration"] = [(vote["configuration"]["pk"],)]
        return keys


register_event_type(SantaBallotEvent)


class SantaTargetStateUpdateEvent(TargetEventMixin, BaseEvent):
    event_type = "santa_target_state_update"
    tags = ["santa"]

    def get_linked_objects_keys(self):
        keys = {}
        self.add_target_to_linked_objects_keys(keys)
        configuration = self.payload.get("configuration")
        if configuration:
            keys["santa_configuration"] = [(configuration["pk"],)]
        return keys


register_event_type(SantaTargetStateUpdateEvent)


def _build_certificate_tree_from_santa_event_cert(in_d):
    out_d = {}
    for from_a, to_a, is_dt in (("cn", "common_name", False),
                                ("org", "organization", False),
                                ("ou", "organizational_unit", False),
                                ("sha256", "sha_256", False),
                                ("valid_from", "valid_from", True),
                                ("valid_until", "valid_until", True)):
        val = in_d.get(from_a)
        if is_dt:
            val = datetime.utcfromtimestamp(val)
        out_d[to_a] = val
    return out_d


def _build_siging_chain_tree_from_santa_event(event_d):
    event_signing_chain = event_d.get("signing_chain")
    if not event_signing_chain:
        return
    signing_chain = None
    current_cert = None
    for in_d in event_signing_chain:
        cert_d = _build_certificate_tree_from_santa_event_cert(in_d)
        if current_cert:
            current_cert["signed_by"] = cert_d
        else:
            signing_chain = cert_d
        current_cert = cert_d
    return signing_chain


def _build_bundle_tree_from_santa_event(event_d):
    bundle_d = {}
    for from_a, to_a in (("file_bundle_id", "bundle_id"),
                         ("file_bundle_name", "bundle_name"),
                         ("file_bundle_version", "bundle_version"),
                         ("file_bundle_version_string", "bundle_version_str")):
        val = event_d.get(from_a)
        if val:
            bundle_d[to_a] = val
    if bundle_d:
        return bundle_d


def _build_file_tree_from_santa_event(event_d):
    app_d = {
        "source": {
            "module": "zentral.contrib.santa",
            "name": "Santa events"
        }
    }
    for from_a, to_a in (("cdhash", "cdhash"),
                         ("file_name", "name"),
                         ("file_path", "path"),
                         ("file_bundle_path", "bundle_path"),
                         ("file_sha256", "sha_256"),
                         ("signing_id", "signing_id")):
        app_d[to_a] = event_d.get(from_a)
    for a, val in (("bundle", _build_bundle_tree_from_santa_event(event_d)),
                   ("signed_by", _build_siging_chain_tree_from_santa_event(event_d))):
        app_d[a] = val
    return app_d


def _is_allow_event(event_d):
    decision = event_d.get('decision')
    return decision and decision.startswith("ALLOW_")


def _is_block_event(event_d):
    decision = event_d.get('decision')
    return decision and decision.startswith("BLOCK_")


def _is_allow_unknown_event(event_d):
    return event_d.get('decision') == "ALLOW_UNKNOWN"


def _is_bundle_binary_pseudo_event(event_d):
    return event_d.get('decision') == "BUNDLE_BINARY"


def _update_targets(configuration, events):
    targets = {}
    for event_d in events:
        # target keys
        target_keys = []
        file_sha256 = event_d.get("file_sha256")
        if file_sha256:
            target_keys.append((Target.Type.BINARY, file_sha256))
        cdhash = event_d.get("cdhash")
        if cdhash:
            target_keys.append((Target.Type.CDHASH, cdhash))
        team_id = event_d.get("team_id")
        if team_id:
            target_keys.append((Target.Type.TEAM_ID, team_id))
        signing_id = event_d.get("signing_id")
        if signing_id:
            target_keys.append((Target.Type.SIGNING_ID, signing_id))
        signing_chain = event_d.get("signing_chain")
        if signing_chain:
            for cert_d in signing_chain:
                sha256 = cert_d.get("sha256")
                if sha256:
                    target_keys.append((Target.Type.CERTIFICATE, sha256))
        if not _is_bundle_binary_pseudo_event(event_d):
            bundle_hash = event_d.get("file_bundle_hash")
            if bundle_hash:
                target_keys.append((Target.Type.BUNDLE, bundle_hash))
        # increments
        blocked_incr = collected_incr = executed_incr = 0
        if _is_block_event(event_d):
            blocked_incr = 1
        elif _is_bundle_binary_pseudo_event(event_d):
            collected_incr = 1
        elif _is_allow_event(event_d):
            executed_incr = 1
        else:
            logger.warning("Unknown decision: %s", event_d.get("decision", "-"))
        # aggregations
        for target_key in target_keys:
            target_increments = targets.setdefault(
                target_key,
                {"blocked_incr": 0, "collected_incr": 0, "executed_incr": 0}
            )
            target_increments["blocked_incr"] += blocked_incr
            target_increments["collected_incr"] += collected_incr
            target_increments["executed_incr"] += executed_incr
    if targets:
        return update_or_create_targets(configuration, targets)
    else:
        return {}


def _create_missing_bundles(events, targets):
    bundle_events = {
        sha256: event_d
        for sha256, event_d in (
            (event_d.get("file_bundle_hash"), event_d)
            for event_d in events
            if not _is_bundle_binary_pseudo_event(event_d)
        )
        if sha256
    }
    if not bundle_events:
        return
    existing_sha256_set = set(
        Bundle.objects.filter(
            target__type=Target.Type.BUNDLE,
            target__identifier__in=bundle_events.keys(),
            uploaded_at__isnull=False,  # to recover from blocked uploads
        ).values_list("target__identifier", flat=True)
    )
    unknown_file_bundle_hashes = list(set(bundle_events.keys()) - existing_sha256_set)
    for sha256 in unknown_file_bundle_hashes:
        target, _ = targets.get((Target.Type.BUNDLE, sha256), (None, None))
        if not target:
            logger.error("Missing BUNDLE target %s", sha256)
            continue
        defaults = {}
        event_d = bundle_events[sha256]
        for event_attr, bundle_attr in (("file_bundle_path", "path"),
                                        ("file_bundle_executable_rel_path", "executable_rel_path"),
                                        ("file_bundle_id", "bundle_id"),
                                        ("file_bundle_name", "name"),
                                        ("file_bundle_version", "version"),
                                        ("file_bundle_version_string", "version_str"),
                                        ("file_bundle_binary_count", "binary_count")):
            val = event_d.get(event_attr)
            if val is None:
                if bundle_attr == "binary_count":
                    val = 0
                else:
                    val = ""
            defaults[bundle_attr] = val
        Bundle.objects.get_or_create(target=target, defaults=defaults)
    return unknown_file_bundle_hashes


def _create_bundle_binaries(events):
    bundle_binary_events = {}
    for event_d in events:
        if _is_bundle_binary_pseudo_event(event_d):
            bundle_sha256 = event_d.get("file_bundle_hash")
            if bundle_sha256:
                bundle_binary_events.setdefault(bundle_sha256, []).append(event_d)
    uploaded_bundles = set()
    for bundle_sha256, events in bundle_binary_events.items():
        try:
            bundle = Bundle.objects.get(target__type=Target.Type.BUNDLE, target__identifier=bundle_sha256)
        except Bundle.DoesNotExist:
            logger.error("Unknown bundle: %s", bundle_sha256)
            continue
        if bundle.uploaded_at:
            logger.error("Bundle %s already uploaded", bundle_sha256)
            continue
        binary_target_identifiers = []
        binary_count = bundle.binary_count
        for event_d in events:
            if not binary_count:
                event_binary_count = event_d.get("file_bundle_binary_count")
                if event_binary_count:
                    binary_count = event_binary_count
            binary_target_identifiers.append(event_d["file_sha256"])
        if binary_target_identifiers:
            add_bundle_binary_targets(bundle, binary_target_identifiers)
        save_bundle = False
        if not bundle.binary_count and binary_count:
            bundle.binary_count = binary_count
            save_bundle = True
        if bundle.binary_count:
            binary_target_count = bundle.binary_targets.count()
            if binary_target_count > bundle.binary_count:
                logger.error("Bundle %s as wrong number of binary targets", bundle_sha256)
            elif binary_target_count == bundle.binary_count:
                bundle.uploaded_at = datetime.utcnow()
                save_bundle = True
                uploaded_bundles.add(bundle)
        if save_bundle:
            bundle.save()
    return uploaded_bundles


def _commit_files(events):
    for event_d in events:
        try:
            file_d = _build_file_tree_from_santa_event(event_d)
        except Exception:
            logger.exception("Could not build app tree from santa event")
        else:
            try:
                File.objects.commit(file_d)
            except Exception:
                logger.exception("Could not commit file")


flatten_events_signing_chain = settings["apps"]["zentral.contrib.santa"].get("flatten_events_signing_chain", True)


def _prepare_santa_event(event_d):
    if flatten_events_signing_chain:
        for i, cert in enumerate(event_d.pop("signing_chain", [])):
            event_d[f"signing_cert_{i}"] = cert
    return event_d


def _post_santa_events(enrolled_machine, user_agent, ip, events):
    def get_created_at(payload):
        return datetime.utcfromtimestamp(payload['execution_time'])

    allow_unknown_shard = enrolled_machine.enrollment.configuration.allow_unknown_shard
    if allow_unknown_shard == 100:
        include_allow_unknown = True
    elif allow_unknown_shard == 0:
        include_allow_unknown = False
    else:
        include_allow_unknown = shard(
            enrolled_machine.serial_number,
            enrolled_machine.enrollment.configuration.pk
        ) <= allow_unknown_shard

    event_iterator = (
        _prepare_santa_event(event_d)
        for event_d in events
        if not _is_bundle_binary_pseudo_event(event_d) and (
            include_allow_unknown or not _is_allow_unknown_event(event_d)
        )
    )

    SantaEventEvent.post_machine_request_payloads(
        enrolled_machine.serial_number, user_agent, ip,
        event_iterator, get_created_at
    )


def process_events(enrolled_machine, user_agent, ip, data):
    events = data.get("events", [])
    if not events:
        return []
    targets = _update_targets(enrolled_machine.enrollment.configuration, events)
    unknown_file_bundle_hashes = _create_missing_bundles(events, targets)
    uploaded_bundles = _create_bundle_binaries(events)
    _commit_files(events)
    if uploaded_bundles:
        logger.info("Update MetaBundles")
        try:
            update_metabundles(uploaded_bundles)
        except Exception:
            logger.exception("Could not update MetaBundles")
    _post_santa_events(enrolled_machine, user_agent, ip, events)
    return unknown_file_bundle_hashes


def post_preflight_event(msn, user_agent, ip, data, incident_update):
    incident_updates = []
    if incident_update is not None:
        incident_updates.append(incident_update)
    event_request = EventRequest(user_agent, ip)
    metadata = EventMetadata(
        machine_serial_number=msn,
        incident_updates=incident_updates,
        request=event_request
    )
    event = SantaPreflightEvent(metadata, data)
    event.post()


def post_enrollment_event(msn, user_agent, ip, data, incident_updates):
    event_request = EventRequest(user_agent, ip)
    metadata = EventMetadata(
        machine_serial_number=msn,
        incident_updates=incident_updates,
        request=event_request
    )
    event = SantaEnrollmentEvent(metadata, data)
    event.post()


def post_santa_rule_update_event(request, data):
    metadata = EventMetadata(request=EventRequest.build_from_request(request))
    event = SantaRuleUpdateEvent(metadata, data)
    event.post()


def post_santa_ruleset_update_events(request, ruleset_data, rules_data):
    event_request = EventRequest.build_from_request(request)
    ruleset_update_event_metadata = EventMetadata(request=event_request)
    ruleset_update_event = SantaRuleSetUpdateEvent(ruleset_update_event_metadata, ruleset_data)
    ruleset_update_event.post()
    for idx, rule_data in enumerate(rules_data):
        rule_update_event_metadata = EventMetadata(request=event_request,
                                                   uuid=ruleset_update_event_metadata.uuid, index=idx + 1)
        rule_update_event = SantaRuleUpdateEvent(rule_update_event_metadata, rule_data)
        rule_update_event.post()
