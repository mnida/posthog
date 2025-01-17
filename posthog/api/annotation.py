from typing import Any, Dict

from django.db.models import Q, QuerySet
from django.db.models.signals import post_save
from django.dispatch import receiver
from rest_framework import filters, request, serializers, viewsets
from rest_framework.permissions import IsAuthenticated
from rest_hooks.signals import raw_hook_event

from posthog.api.forbid_destroy_model import ForbidDestroyModel
from posthog.api.routing import StructuredViewSetMixin
from posthog.api.shared import UserBasicSerializer
from posthog.event_usage import report_user_action
from posthog.models import Annotation, Team
from posthog.permissions import ProjectMembershipNecessaryPermissions, TeamMemberAccessPermission


class AnnotationSerializer(serializers.ModelSerializer):
    created_by = UserBasicSerializer(read_only=True)

    class Meta:
        model = Annotation
        fields = [
            "id",
            "content",
            "date_marker",
            "creation_type",
            "dashboard_item",
            "insight_short_id",
            "insight_name",
            "created_by",
            "created_at",
            "updated_at",
            "deleted",
            "scope",
        ]
        read_only_fields = [
            "id",
            "creation_type",
            "insight_short_id",
            "insight_name",
            "created_by",
            "created_at",
            "updated_at",
        ]

    def update(self, instance: Annotation, validated_data: Dict[str, Any]) -> Annotation:
        instance.team_id = self.context["team_id"]
        return super().update(instance, validated_data)

    def create(self, validated_data: Dict[str, Any], *args: Any, **kwargs: Any) -> Annotation:
        request = self.context["request"]
        project = Team.objects.get(id=self.context["team_id"])
        annotation = Annotation.objects.create(
            organization=project.organization, team=project, created_by=request.user, **validated_data,
        )
        return annotation


class AnnotationsViewSet(StructuredViewSetMixin, ForbidDestroyModel, viewsets.ModelViewSet):
    """
    Create, Read, Update and Delete annotations. [See docs](https://posthog.com/docs/user-guides/annotations) for more information on annotations.
    """

    queryset = Annotation.objects.select_related("dashboard_item")
    serializer_class = AnnotationSerializer
    permission_classes = [IsAuthenticated, ProjectMembershipNecessaryPermissions, TeamMemberAccessPermission]
    filter_backends = [filters.SearchFilter]
    search_fields = ["content"]

    def get_queryset(self) -> QuerySet:
        queryset = super().get_queryset()
        if self.action == "list":
            order = self.request.GET.get("order", "-date_marker")
            queryset = self._filter_request(self.request, queryset).order_by(order)
        if self.action != "partial_update":
            # We never want deleted items to be included in the queryset… except when we want to restore an annotation
            # Annotations are restored with a PATCH request setting `deleted` to `False`
            queryset = queryset.filter(deleted=False)

        return queryset

    def filter_queryset_by_parents_lookups(self, queryset):
        parents_query_dict = self.parents_query_dict.copy()
        organization_id = self.team.organization_id

        return queryset.filter(
            Q(team_id=parents_query_dict["team_id"])
            | Q(scope=Annotation.Scope.ORGANIZATION, organization_id=organization_id)
        )

    def _filter_request(self, request: request.Request, queryset: QuerySet) -> QuerySet:
        filters = request.GET.dict()

        for key in filters:
            if key == "after":
                queryset = queryset.filter(created_at__gt=request.GET["after"])
            elif key == "before":
                queryset = queryset.filter(created_at__lt=request.GET["before"])
            elif key == "dashboardItemId":
                queryset = queryset.filter(dashboard_item_id=request.GET["dashboardItemId"])
            elif key == "scope":
                queryset = queryset.filter(scope=request.GET["scope"])

        return queryset


@receiver(post_save, sender=Annotation, dispatch_uid="hook-annotation-created")
def annotation_created(sender, instance, created, raw, using, **kwargs):
    """Trigger action_defined hooks on Annotation creation."""

    if created:
        raw_hook_event.send(
            sender=None,
            event_name="annotation_created",
            instance=instance,
            payload=AnnotationSerializer(instance).data,
            user=instance.team,
        )

    if instance.created_by:
        event_name: str = "annotation created" if created else "annotation updated"
        report_user_action(instance.created_by, event_name, instance.get_analytics_metadata())
