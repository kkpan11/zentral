import base64
import logging
import os
from django.core.files import File
from django.db import transaction
from rest_framework import serializers
from zentral.contrib.inventory.models import EnrollmentSecret, Tag
from zentral.contrib.inventory.serializers import EnrollmentSecretSerializer
from zentral.utils.os_version import make_comparable_os_version
from zentral.utils.ssl import ensure_bytes
from .app_manifest import download_package, read_package_info, validate_configuration
from .artifacts import update_blueprint_serialized_artifacts
from .crypto import generate_push_certificate_key_bytes, load_push_certificate_and_key
from .dep import assign_dep_device_profile, DEPClientError
from .models import (Artifact, ArtifactVersion, ArtifactVersionTag,
                     Blueprint, BlueprintArtifact, BlueprintArtifactTag,
                     DEPDevice, DEPEnrollment,
                     DeviceCommand,
                     EnrolledDevice, EnterpriseApp, FileVaultConfig,
                     Location, LocationAsset,
                     OTAEnrollment,
                     Platform, Profile, PushCertificate,
                     RecoveryPasswordConfig,
                     SCEPConfig,
                     SoftwareUpdateEnforcement)
from .payloads import get_configuration_profile_info
from .scep.microsoft_ca import MicrosoftCAChallengeSerializer, OktaCAChallengeSerializer
from .scep.static import StaticChallengeSerializer


logger = logging.getLogger("zentral.contrib.mdm.serializers")


class DeviceCommandSerializer(serializers.ModelSerializer):
    class Meta:
        model = DeviceCommand
        fields = (
            "id",
            "uuid",
            "enrolled_device",
            "name",
            "artifact_version",
            "artifact_operation",
            "not_before",
            "time",
            "result",
            "result_time",
            "status",
            "error_chain",
            "created_at",
            "updated_at"
        )


class EnrolledDeviceSerializer(serializers.ModelSerializer):
    os_version = serializers.CharField(source="current_os_version")
    build_version = serializers.CharField(source="current_build_version")

    class Meta:
        model = EnrolledDevice
        fields = (
            "id",
            "udid",
            "serial_number",
            "name",
            "model",
            "platform",
            "os_version",
            "build_version",
            "apple_silicon",
            "cert_not_valid_after",
            "blueprint",
            "awaiting_configuration",
            "declarative_management",
            "dep_enrollment",
            "user_enrollment",
            "user_approved_enrollment",
            "supervised",
            "bootstrap_token_escrowed",
            "filevault_enabled",
            "filevault_prk_escrowed",
            "recovery_password_escrowed",
            "activation_lock_manageable",
            "last_seen_at",
            "last_notified_at",
            "checkout_at",
            "blocked_at",
            "created_at",
            "updated_at",
        )


class ArtifactSerializer(serializers.ModelSerializer):
    class Meta:
        model = Artifact
        fields = "__all__"

    def update(self, instance, validated_data):
        with transaction.atomic(durable=True):
            instance = super().update(instance, validated_data)
        with transaction.atomic(durable=True):
            for blueprint in instance.blueprints():
                update_blueprint_serialized_artifacts(blueprint)
        return instance


class FileVaultConfigSerializer(serializers.ModelSerializer):
    class Meta:
        model = FileVaultConfig
        fields = "__all__"

    def validate(self, data):
        bypass_attempts = data.get("bypass_attempts", -1)
        if data.get("at_login_only", False):
            if bypass_attempts < 0:
                raise serializers.ValidationError({"bypass_attempts": "Must be >= 0 when at_login_only is True"})
        elif bypass_attempts > -1:
            raise serializers.ValidationError({"bypass_attempts": "Must be -1 when at_login_only is False"})
        return data


class DEPDeviceSerializer(serializers.ModelSerializer):
    class Meta:
        model = DEPDevice
        fields = [
            "id",
            "virtual_server", "serial_number",
            "asset_tag", "color",
            "description", "device_family",
            "model", "os",
            "device_assigned_by", "device_assigned_date",
            "last_op_type", "last_op_date",
            "profile_status", "profile_uuid", "profile_push_time",
            "enrollment",
            "created_at", "updated_at",
        ]
        read_only_fields = [
            "id",
            "virtual_server", "serial_number",
            "asset_tag", "color",
            "description", "device_family",
            "model", "os",
            "device_assigned_by", "device_assigned_date",
            "last_op_type", "last_op_date",
            "profile_status", "profile_uuid", "profile_push_time",
            "created_at", "updated_at",
        ]

    def update(self, instance, validated_data):
        enrollment = validated_data.pop("enrollment")
        try:
            assign_dep_device_profile(instance, enrollment)
        except DEPClientError:
            logger.exception("Could not assign enrollment to device")
            raise serializers.ValidationError({"enrollment": "Could not assign enrollment to device"})
        else:
            instance.enrollment = enrollment
        return super().update(instance, validated_data)


class OTAEnrollmentSerializer(serializers.ModelSerializer):
    enrollment_secret = EnrollmentSecretSerializer(many=False)

    class Meta:
        model = OTAEnrollment
        fields = "__all__"

    def create(self, validated_data):
        secret_data = validated_data.pop('enrollment_secret')
        secret_tags = secret_data.pop("tags", [])
        secret = EnrollmentSecret.objects.create(**secret_data)
        if secret_tags:
            secret.tags.set(secret_tags)
        return OTAEnrollment.objects.create(enrollment_secret=secret, **validated_data)

    def update(self, instance, validated_data):
        secret_serializer = self.fields["enrollment_secret"]
        secret_data = validated_data.pop('enrollment_secret')
        secret_serializer.update(instance.enrollment_secret, secret_data)
        return super().update(instance, validated_data)


class PushCertificateSerializer(serializers.ModelSerializer):
    class Meta:
        model = PushCertificate
        fields = (
            "id",
            "provisioning_uid",
            "name",
            "topic",
            "not_before",
            "not_after",
            "certificate",
            "created_at",
            "updated_at"
        )

    def to_internal_value(self, data):
        # We need to implement this to keep the certificate
        # and apply it only if it is provided in the uploaded data.
        # There is no reason to nullify the certificate!
        certificate = data.pop("certificate", None)
        data = super().to_internal_value(data)
        if certificate:
            data["certificate"] = certificate
        return data

    def to_representation(self, instance):
        ret = super().to_representation(instance)
        if instance.certificate:
            ret["certificate"] = ensure_bytes(instance.certificate).decode("ascii")
        return ret

    def validate(self, data):
        certificate = data.pop("certificate", None)
        if certificate:
            if not self.instance:
                raise serializers.ValidationError("Certificate cannot be set when creating a push certificate")
            try:
                push_certificate_d = load_push_certificate_and_key(
                    certificate,
                    self.instance.get_private_key(),
                )
            except ValueError as e:
                raise serializers.ValidationError(str(e))
            if self.instance.topic:
                if push_certificate_d["topic"] != self.instance.topic:
                    raise serializers.ValidationError("The new certificate has a different topic")
            else:
                if PushCertificate.objects.filter(topic=push_certificate_d["topic"]).exists():
                    raise serializers.ValidationError("A different certificate with the same topic already exists")
            push_certificate_d.pop("private_key")
            data.update(push_certificate_d)
        return data

    def create(self, validated_data):
        instance = super().create(validated_data)
        instance.set_private_key(generate_push_certificate_key_bytes())
        instance.save()
        return instance


class RecoveryPasswordConfigSerializer(serializers.ModelSerializer):
    static_password = serializers.CharField(required=False, source="get_static_password", allow_null=True)

    class Meta:
        model = RecoveryPasswordConfig
        fields = ("id", "name",
                  "dynamic_password", "static_password",
                  "rotation_interval_days", "rotate_firmware_password",
                  "created_at", "updated_at")

    def validate(self, data):
        dynamic_password = data.get("dynamic_password", True)
        static_password = data.get("get_static_password")
        rotation_interval_days = data.get("rotation_interval_days")
        rotate_firmware_password = data.get("rotate_firmware_password")
        errors = {}
        if dynamic_password:
            if static_password:
                errors["static_password"] = "Cannot be set when dynamic_password is true"
        else:
            if not static_password:
                errors["static_password"] = "Required when dynamic_password is false"
            if rotation_interval_days:
                errors["rotation_interval_days"] = "Cannot be set with a static password"
            if rotate_firmware_password:
                errors["rotate_firmware_password"] = "Cannot be set with a static password"
        if rotate_firmware_password and not rotation_interval_days:
            errors["rotate_firmware_password"] = "Cannot be set without a rotation interval"
        if errors:
            raise serializers.ValidationError(errors)
        return data

    def create(self, validated_data):
        static_password = validated_data.pop("get_static_password", None)
        instance = RecoveryPasswordConfig.objects.create(**validated_data)
        if static_password:
            instance.set_static_password(static_password)
            instance.save()
        return instance

    def update(self, instance, validated_data):
        static_password = validated_data.pop("get_static_password", None)
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        instance.set_static_password(static_password)
        instance.save()
        return instance


class SCEPConfigSerializer(serializers.ModelSerializer):
    microsoft_ca_challenge_kwargs = MicrosoftCAChallengeSerializer(
        source="get_microsoft_ca_challenge_kwargs",
        required=False,
    )
    okta_ca_challenge_kwargs = OktaCAChallengeSerializer(
        source="get_okta_ca_challenge_kwargs",
        required=False,
    )
    static_challenge_kwargs = StaticChallengeSerializer(
        source="get_static_challenge_kwargs",
        required=False,
    )

    class Meta:
        model = SCEPConfig
        fields = (
            "id",
            "provisioning_uid",
            "name",
            "url",
            "key_usage",
            "key_is_extractable",
            "keysize",
            "allow_all_apps_access",
            "challenge_type",
            "microsoft_ca_challenge_kwargs",
            "okta_ca_challenge_kwargs",
            "static_challenge_kwargs",
            "created_at",
            "updated_at",
        )

    def validate(self, data):
        data = super().validate(data)
        challenge_type = data.get("challenge_type")
        if challenge_type:
            field_name = f"{challenge_type.lower()}_challenge_kwargs"
            data["challenge_kwargs"] = data.pop(f"get_{field_name}", {})
            if not data["challenge_kwargs"]:
                raise serializers.ValidationError({field_name: "This field is required."})
        return data

    def create(self, validated_data):
        challenge_kwargs = validated_data.pop("challenge_kwargs", {})
        validated_data["challenge_kwargs"] = {}
        scep_config = super().create(validated_data)
        scep_config.set_challenge_kwargs(challenge_kwargs)
        scep_config.save()
        return scep_config

    def update(self, instance, validated_data):
        challenge_kwargs = validated_data.pop("challenge_kwargs", {})
        scep_config = super().update(instance, validated_data)
        scep_config.set_challenge_kwargs(challenge_kwargs)
        scep_config.save()
        return scep_config

    def to_representation(self, instance):
        ret = super().to_representation(instance)
        if instance.provisioning_uid:
            for field in list(ret.keys()):
                if "challenge" in field:
                    ret.pop(field)
        return ret


class SoftwareUpdateEnforcementSerializer(serializers.ModelSerializer):
    latest_fields = ("max_os_version", "delay_days", "local_time")
    one_time_fields = ("os_version", "build_version", "local_datetime")

    class Meta:
        model = SoftwareUpdateEnforcement
        fields = "__all__"

    def _validate_os_version(self, value):
        if value and make_comparable_os_version(value) == (0, 0, 0):
            raise serializers.ValidationError("Not a valid OS version")
        return value

    def validate_max_os_version(self, value):
        return self._validate_os_version(value)

    def validate_os_version(self, value):
        return self._validate_os_version(value)

    def validate(self, data):
        max_os_version = data.get("max_os_version")
        os_version = data.get("os_version")
        if max_os_version and os_version:
            raise serializers.ValidationError("os_version and max_os_version cannot be both set")
        if max_os_version:
            mode = "max_os_version"
            required_fields = (f for f in self.latest_fields if f not in ("delay_days", "local_time"))
            other_fields = self.one_time_fields
        elif os_version:
            mode = "os_version"
            required_fields = (f for f in self.one_time_fields if f != "build_version")
            other_fields = self.latest_fields
        else:
            raise serializers.ValidationError("os_version or max_os_version are required")
        errors = {}
        for field in required_fields:
            value = data.get(field)
            if value is None or value == "":
                errors[field] = f"This field is required if {mode} is used"
        for field in other_fields:
            if data.get(field):
                errors[field] = f"This field cannot be set if {mode} is used"
            else:
                data[field] = "" if field not in ("delay_days", "local_time", "local_datetime") else None
        if errors:
            raise serializers.ValidationError(errors)
        return data


class BlueprintSerializer(serializers.ModelSerializer):
    class Meta:
        model = Blueprint
        exclude = ["serialized_artifacts"]


class FilteredBlueprintItemTagSerializer(serializers.Serializer):
    tag = serializers.PrimaryKeyRelatedField(queryset=Tag.objects.all())
    shard = serializers.IntegerField(min_value=1, max_value=100)


def validate_filtered_blueprint_item_data(data):
    # platforms & min max versions
    platform_active = False
    if not data:
        return
    artifact = data.get("artifact")
    for platform in Platform.values:
        field = platform.lower()
        if data.get(field, False):
            platform_active = True
            if artifact and platform not in artifact.platforms:
                raise serializers.ValidationError({field: "Platform not available for this artifact"})
    if not platform_active:
        raise serializers.ValidationError("You need to activate at least one platform")
    # shards
    shard_modulo = data.get("shard_modulo")
    default_shard = data.get("default_shard")
    if isinstance(shard_modulo, int) and isinstance(default_shard, int) and default_shard > shard_modulo:
        raise serializers.ValidationError({"default_shard": "Must be less than or equal to the shard modulo"})
    # excluded tags
    excluded_tags = data.get("excluded_tags", [])
    # tag shards
    for tag_shard in data.get("tag_shards", []):
        tag = tag_shard.get("tag")
        if tag and tag in excluded_tags:
            raise serializers.ValidationError({"excluded_tags": f"Tag {tag} also present in the tag shards"})
        shard = tag_shard.get("shard")
        if isinstance(shard, int) and isinstance(shard_modulo, int) and shard > shard_modulo:
            raise serializers.ValidationError({"tag_shards": f"Shard for tag {tag} > shard modulo"})


class BlueprintArtifactSerializer(serializers.ModelSerializer):
    excluded_tags = serializers.PrimaryKeyRelatedField(queryset=Tag.objects.all(), many=True,
                                                       default=list, required=False)
    tag_shards = FilteredBlueprintItemTagSerializer(many=True, default=list, required=False)

    class Meta:
        model = BlueprintArtifact
        fields = "__all__"

    def validate(self, data):
        validate_filtered_blueprint_item_data(data)
        return data

    def create(self, validated_data):
        tag_shards = validated_data.pop("tag_shards")
        with transaction.atomic(durable=True):
            instance = super().create(validated_data)
            for tag_shard in tag_shards:
                BlueprintArtifactTag.objects.create(blueprint_artifact=instance, **tag_shard)
        with transaction.atomic(durable=True):
            update_blueprint_serialized_artifacts(instance.blueprint)
        return instance

    def update(self, instance, validated_data):
        tag_shard_dict = {tag_shard["tag"]: tag_shard["shard"] for tag_shard in validated_data.pop("tag_shards")}
        with transaction.atomic(durable=True):
            instance = super().update(instance, validated_data)
            instance.item_tags.exclude(tag__in=tag_shard_dict.keys()).delete()
            for tag, shard in tag_shard_dict.items():
                BlueprintArtifactTag.objects.update_or_create(
                    blueprint_artifact=instance,
                    tag=tag,
                    defaults={"shard": shard}
                )
        with transaction.atomic(durable=True):
            update_blueprint_serialized_artifacts(instance.blueprint)
        return instance


class ArtifactVersionSerializer(serializers.Serializer):
    id = serializers.UUIDField(read_only=True, source="artifact_version.pk")
    artifact = serializers.PrimaryKeyRelatedField(queryset=Artifact.objects.all(),
                                                  source="artifact_version.artifact")
    ios = serializers.BooleanField(required=False, default=False,
                                   source="artifact_version.ios")
    ios_min_version = serializers.CharField(required=False, default="", allow_blank=True,
                                            source="artifact_version.ios_min_version")
    ios_max_version = serializers.CharField(required=False, default="", allow_blank=True,
                                            source="artifact_version.ios_max_version")
    ipados = serializers.BooleanField(required=False, default=False,
                                      source="artifact_version.ipados")
    ipados_min_version = serializers.CharField(required=False, default="", allow_blank=True,
                                               source="artifact_version.ipados_min_version")
    ipados_max_version = serializers.CharField(required=False, default="", allow_blank=True,
                                               source="artifact_version.ipados_max_version")
    macos = serializers.BooleanField(required=False, default=False,
                                     source="artifact_version.macos")
    macos_min_version = serializers.CharField(required=False, default="", allow_blank=True,
                                              source="artifact_version.macos_min_version")
    macos_max_version = serializers.CharField(required=False, default="", allow_blank=True,
                                              source="artifact_version.macos_max_version")
    tvos = serializers.BooleanField(required=False, default=False,
                                    source="artifact_version.tvos")
    tvos_min_version = serializers.CharField(required=False, default="", allow_blank=True,
                                             source="artifact_version.tvos_min_version")
    tvos_max_version = serializers.CharField(required=False, default="", allow_blank=True,
                                             source="artifact_version.tvos_max_version")
    shard_modulo = serializers.IntegerField(min_value=1, max_value=100, default=100,
                                            source="artifact_version.shard_modulo")
    default_shard = serializers.IntegerField(min_value=0, max_value=100, default=100,
                                             source="artifact_version.default_shard")
    excluded_tags = serializers.PrimaryKeyRelatedField(queryset=Tag.objects.all(), many=True,
                                                       default=list, required=False,
                                                       source="artifact_version.excluded_tags")
    tag_shards = FilteredBlueprintItemTagSerializer(many=True,
                                                    default=list, required=False,
                                                    source="artifact_version.tag_shards")
    version = serializers.IntegerField(min_value=1, source="artifact_version.version")
    created_at = serializers.DateTimeField(read_only=True, source="artifact_version.created_at")
    updated_at = serializers.DateTimeField(read_only=True, source="artifact_version.updated_at")

    def validate(self, data):
        # filters
        artifact_version = data.get("artifact_version")
        validate_filtered_blueprint_item_data(artifact_version)
        # version conflict
        artifact = artifact_version.get("artifact")
        version = artifact_version.get("version")
        if artifact and isinstance(version, int):
            version_conflict_qs = artifact.artifactversion_set.filter(version=version)
            if self.instance is not None:
                version_conflict_qs = version_conflict_qs.exclude(pk=self.instance.artifact_version.pk)
            if version_conflict_qs.count():
                raise serializers.ValidationError(
                    {"version": "A version of this artifact with the same version number already exists"}
                )
        return data

    def create(self, validated_data):
        data = validated_data.pop("artifact_version")
        excluded_tags = data.pop("excluded_tags")
        tag_shards = data.pop("tag_shards")
        artifact_version = ArtifactVersion.objects.create(**data)
        artifact_version.excluded_tags.set(excluded_tags)
        for tag_shard in tag_shards:
            ArtifactVersionTag.objects.create(artifact_version=artifact_version, **tag_shard)
        return artifact_version

    def update(self, instance, validated_data):
        data = validated_data.pop("artifact_version")
        excluded_tags = data.pop("excluded_tags")
        tag_shard_dict = {tag_shard["tag"]: tag_shard["shard"] for tag_shard in data.pop("tag_shards")}
        artifact_version = instance.artifact_version
        for attr, value in data.items():
            setattr(artifact_version, attr, value)
        artifact_version.save()
        artifact_version.excluded_tags.set(excluded_tags)
        artifact_version.item_tags.exclude(tag__in=tag_shard_dict.keys()).delete()
        for tag, shard in tag_shard_dict.items():
            ArtifactVersionTag.objects.update_or_create(
                artifact_version=artifact_version,
                tag=tag,
                defaults={"shard": shard}
            )
        return artifact_version


class B64EncodedBinaryField(serializers.Field):
    def to_representation(self, value):
        return base64.b64encode(value).decode("ascii")

    def to_internal_value(self, data):
        return base64.b64decode(data)


class ProfileSerializer(ArtifactVersionSerializer):
    source = B64EncodedBinaryField()

    def validate(self, data):
        data = super().validate(data)
        source = data.pop("source", None)
        if source is None:
            return data
        try:
            source, info = get_configuration_profile_info(source)
        except ValueError as e:
            raise serializers.ValidationError({"source": str(e)})
        data["profile"] = info
        data["profile"]["source"] = source
        data["profile"].pop("channel")
        return data

    def create(self, validated_data):
        with transaction.atomic(durable=True):
            artifact_version = super().create(validated_data)
            instance = Profile.objects.create(
                artifact_version=artifact_version,
                **validated_data["profile"]
            )
        with transaction.atomic(durable=True):
            for blueprint in artifact_version.artifact.blueprints():
                update_blueprint_serialized_artifacts(blueprint)
        return instance

    def update(self, instance, validated_data):
        with transaction.atomic(durable=True):
            super().update(instance, validated_data)
            for attr, value in validated_data["profile"].items():
                setattr(instance, attr, value)
            instance.save()
        with transaction.atomic(durable=True):
            for blueprint in instance.artifact_version.artifact.blueprints():
                update_blueprint_serialized_artifacts(blueprint)
        return instance


class EnterpriseAppSerializer(ArtifactVersionSerializer):
    package_uri = serializers.CharField(required=True)
    package_sha256 = serializers.CharField(required=True)
    package_size = serializers.IntegerField(read_only=True)
    filename = serializers.CharField(read_only=True)
    product_id = serializers.CharField(read_only=True)
    product_version = serializers.CharField(read_only=True)
    configuration = serializers.CharField(required=False, source="get_configuration_plist",
                                          default=None, allow_null=True)
    bundles = serializers.JSONField(read_only=True)
    manifest = serializers.JSONField(read_only=True)
    ios_app = serializers.BooleanField(required=False, default=False)
    install_as_managed = serializers.BooleanField(required=False, default=False)
    remove_on_unenroll = serializers.BooleanField(required=False, default=False)

    def validate_configuration(self, value):
        try:
            return validate_configuration(value)
        except ValueError as e:
            raise serializers.ValidationError(str(e))

    def validate(self, data):
        data = super().validate(data)
        if data.get("remove_on_unenroll") and not data.get("install_as_managed"):
            raise serializers.ValidationError({
                "remove_on_unenroll": "Only available if installed as managed is also set"
            })
        package_uri = data.get("package_uri")
        if package_uri is None:
            return data
        package_sha256 = data.get("package_sha256")
        if package_sha256 is None:
            return data
        try:
            filename, tmp_file = download_package(package_uri, package_sha256)
            _, _, ea_data = read_package_info(tmp_file)
        except Exception as e:
            raise serializers.ValidationError({"package_uri": str(e)})
        # same product ID?
        artifact = data["artifact_version"]["artifact"]
        if (
            EnterpriseApp.objects.filter(artifact_version__artifact=artifact)
                                 .exclude(product_id=ea_data["product_id"]).exists()
        ):
            raise serializers.ValidationError(
                {"package_uri": "The product ID of the new app is not identical "
                                "to the product ID of the other versions"}
            )
        # non-field attributes
        ea_data["filename"] = filename
        ea_data["package"] = File(tmp_file)
        # field attributes
        for attr in ("package_uri", "package_sha256",
                     "ios_app", "configuration",
                     "install_as_managed", "remove_on_unenroll"):
            if attr == "configuration":
                data_attr = "get_configuration_plist"
            else:
                data_attr = attr
            ea_data[attr] = data.pop(data_attr)
        data["enterprise_app"] = ea_data
        return data

    def create(self, validated_data):
        try:
            with transaction.atomic(durable=True):
                artifact_version = super().create(validated_data)
                instance = EnterpriseApp.objects.create(
                    artifact_version=artifact_version,
                    **validated_data["enterprise_app"]
                )
            with transaction.atomic(durable=True):
                for blueprint in artifact_version.artifact.blueprints():
                    update_blueprint_serialized_artifacts(blueprint)
        finally:
            os.unlink(validated_data["enterprise_app"]["package"].name)
        return instance

    def update(self, instance, validated_data):
        try:
            with transaction.atomic(durable=True):
                super().update(instance, validated_data)
                for attr, value in validated_data["enterprise_app"].items():
                    setattr(instance, attr, value)
                instance.save()
            with transaction.atomic(durable=True):
                for blueprint in instance.artifact_version.artifact.blueprints():
                    update_blueprint_serialized_artifacts(blueprint)
        finally:
            os.unlink(validated_data["enterprise_app"]["package"].name)
        return instance


class LocationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Location
        fields = (
            "id",
            "server_token_expiration_date",
            "organization_name",
            "name",
            "country_code",
            "library_uid",
            "platform",
            "website_url",
            "mdm_info_id",
            "created_at",
            "updated_at",
        )


class LocationAssetSerializer(serializers.ModelSerializer):
    class Meta:
        model = LocationAsset
        fields = "__all__"
