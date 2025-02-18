from django.shortcuts import get_object_or_404
from django.urls import reverse
from django_filters import rest_framework as filters
from rest_framework import status
from rest_framework.authentication import SessionAuthentication
from rest_framework.filters import OrderingFilter
from rest_framework.generics import ListAPIView, RetrieveUpdateAPIView
from rest_framework.pagination import LimitOffsetPagination
from rest_framework.views import APIView
from rest_framework.response import Response
from accounts.api_authentication import APITokenAuthentication
from zentral.contrib.mdm.models import DEPDevice, DEPVirtualServer
from zentral.contrib.mdm.tasks import sync_dep_virtual_server_devices_task
from zentral.contrib.mdm.serializers import DEPDeviceSerializer
from zentral.utils.drf import DefaultDjangoModelPermissions, DjangoPermissionRequired


class DEPVirtualServerSyncDevicesView(APIView):
    authentication_classes = [APITokenAuthentication, SessionAuthentication]
    permission_required = "mdm.view_depvirtualserver"
    permission_classes = [DjangoPermissionRequired]

    def post(self, request, *args, **kwargs):
        server = get_object_or_404(DEPVirtualServer, pk=kwargs["pk"])
        result = sync_dep_virtual_server_devices_task.apply_async((server.pk,))
        return Response({"task_id": result.id,
                         "task_result_url": reverse("base_api:task_result", args=(result.id,))},
                        status=status.HTTP_201_CREATED)


class MaxLimitOffsetPagination(LimitOffsetPagination):
    default_limit = 50
    max_limit = 500


class DEPDeviceList(ListAPIView):
    queryset = DEPDevice.objects.all().order_by("-created_at")
    serializer_class = DEPDeviceSerializer
    permission_classes = [DefaultDjangoModelPermissions]
    filter_backends = (filters.DjangoFilterBackend, OrderingFilter)
    filterset_fields = (
        'device_family',
        'enrollment', 'profile_status', 'profile_uuid',
        'serial_number', 'virtual_server'
    )
    ordering_fields = ('created_at', 'last_op_date', 'updated_at')
    ordering = ['-created_at']
    pagination_class = MaxLimitOffsetPagination


class DEPDeviceDetail(RetrieveUpdateAPIView):
    queryset = DEPDevice.objects.all()
    serializer_class = DEPDeviceSerializer
    permission_classes = [DefaultDjangoModelPermissions]
