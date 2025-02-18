import logging
from django import forms
from rest_framework import serializers
from . import SCEPChallengeType
from .base import SCEPChallenge


logger = logging.getLogger("zentral.contrib.mdm.scep.static")


class StaticChallengeForm(forms.Form):
    challenge = forms.CharField(widget=forms.PasswordInput(render_value=True))


class StaticChallengeSerializer(serializers.Serializer):
    challenge = serializers.CharField()

    def get_challenge_kwargs(self):
        return {k: v for k, v in self.cleaned_data.items() if v}


class StaticChallenge(SCEPChallenge):
    type = SCEPChallengeType.STATIC
    kwargs_keys = ("challenge",)
    encrypted_kwargs_keys = ("challenge",)
    form_class = StaticChallengeForm

    def get(self, key_usage, subject, subject_alt_name):
        return self.challenge
