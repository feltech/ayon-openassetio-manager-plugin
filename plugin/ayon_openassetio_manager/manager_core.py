"""Core implementation of the AYON OpenAssetIO Manager Interface."""
from __future__ import annotations

import dataclasses
import pathlib
from pathlib import Path
from threading import Lock
from typing import Any, Callable, Optional, Union, cast

import ayon_api
import openassetio
import openassetio_mediacreation.traits as mc_traits
from ayon_core import pipeline
from cachetools import TTLCache
from openassetio import Context, EntityReference, access
from openassetio.errors import BatchElementError
from openassetio.managerApi import (
    EntityReferencePagerInterface,
    HostSession,
    ManagerStateBase,
)
from openassetio.trait import TraitsData
from openassetio.utils import FileUrlPathConverter
from openassetio_mediacreation.traits.managementPolicy import ManagedTrait

from . import ayon, ayon_core_util

InfoDictionary = dict[str, Union[str, float, int, bool]]


class AyonManagerState(ManagerStateBase):
    """Ayon-specific manager state.

    Can be used to cache project settings, etc.
    """
    # noinspection PyMissingConstructor
    def __init__(
            self, settings: InfoDictionary, entity_info: ayon.EntityInfo):
        """Constructor."""
        ManagerStateBase.__init__(self)
        self.most_recent_resolved_entity_info = entity_info
        self.settings = settings


class AyonOpenAssetIOManagerInterfaceCore:
    """Implementation of the AYON OpenAssetIO Manager Interface.

    This class contains the actual implementation of the manager interface,
    while AyonOpenAssetIOManagerInterface acts as a proxy that delegates
    method calls to this class.

    This is due to the requirement for ayon_core to be imported at runtime.
    I.e. this class will not be imported at startup, but only when the
    AyonOpenAssetIOManagerInterface is `initialize()`d.
    """
    __reference_prefix = "ayon+entity://"

    def __init__(self, settings: InfoDictionary):
        """Constructor."""
        self.__settings = settings
        self.__fileUrlPathConverter = FileUrlPathConverter()
        self.__resolve_cache_lock = Lock()
        self.__resolve_cache = TTLCache(maxsize=1000, ttl=3)

    def createState(  # noqa: N802
            self, _host_session: HostSession) -> AyonManagerState:
        """Create initial manager state.

        Args:
            _host_session (HostSession): The host session.

        Returns:
            AyonManagerState: The initial manager state.

        """
        default_entity_info = ayon.EntityInfo(
            project_name=self.__settings.get(ayon.PROJECT_NAME_KEY),
            path=self.__settings.get(ayon.PATH_NAME_KEY),
            task_name=self.__settings.get(ayon.TASK_NAME_KEY),
            uri=""
        )

        return AyonManagerState(self.__settings, default_entity_info)

    @staticmethod
    def createChildState(  # noqa: N802
        parent_state: AyonManagerState, _host_session: HostSession
    ) -> AyonManagerState:
        """Create a child manager state from a parent state.

        Args:
            parent_state (AyonManagerState): The parent manager state.
            _host_session (HostSession): The host session.

        Returns:
            AyonManagerState: The child manager state.

        """
        return parent_state

    @staticmethod
    def managementPolicy(  # noqa: N802, C901
        trait_sets: list[set[str]],
        policy_access: access.PolicyAccess,
        _context: openassetio.Context,
        _host_session: openassetio.managerApi.HostSession,
    ) -> list[TraitsData]:
        """Get management policies for the given trait sets and access mode.

        Args:
            trait_sets (List[Set[str]]): The trait sets to get policies for.
            policy_access (access.PolicyAccess): The access mode.
            _context (Context): The context.
            _host_session (HostSession): The host session.

        Returns:
            List[TraitsData]: The management policies for the given trait sets.

        """
        policies = [TraitsData() for _ in trait_sets]

        for trait_set, policy in zip(trait_sets, policies):
            # TODO(DF): We should also disallow resolving/publishing traits
            #  based on the other traits in the trait set (or rather, based on
            #  defined Specifications). At the moment we're saying e.g. that we
            #  can resolve the OCIO colour space of audio, which probably
            #  doesn't make sense.

            if policy_access in {
                access.PolicyAccess.kRead,
                access.PolicyAccess.kWrite,
                access.PolicyAccess.kCreateRelated,
            }:
                if mc_traits.content.LocatableContentTrait.kId in trait_set:
                    mc_traits.content.LocatableContentTrait.imbueTo(policy)

                if mc_traits.color.OCIOColorManagedTrait.kId in trait_set:
                    mc_traits.color.OCIOColorManagedTrait.imbueTo(policy)

                if mc_traits.timeDomain.FrameRangedTrait.kId in trait_set:
                    mc_traits.timeDomain.FrameRangedTrait.imbueTo(policy)

            if policy_access == access.PolicyAccess.kManagerDriven:
                if mc_traits.content.LocatableContentTrait.kId in trait_set:
                    mc_traits.content.LocatableContentTrait.imbueTo(policy)

            # Here we're saying that, regardless of the asset type, if you want
            # to publish it then we need a location for it. This should change
            # if we can update/create other metadata without also updating the
            # location.
            if policy_access == access.PolicyAccess.kRequired:
                if mc_traits.content.LocatableContentTrait.kId in trait_set:
                    mc_traits.content.LocatableContentTrait.imbueTo(policy)

            if policy.traitSet():
                ManagedTrait.imbueTo(policy)

        return policies

    def isEntityReferenceString(  # noqa: N802
            self, some_string: str, _host_session: HostSession) -> bool:
        """Check if a string is an AYON entity reference.

        Args:
            some_string (str): The string to check.
            _host_session (HostSession): The host session.

        Returns:
            bool: True if the string is an AYON entity reference,
                False otherwise.

        """
        return some_string.startswith(self.__reference_prefix)

    @staticmethod
    def entityExists(  # noqa: N802
        entity_refs: list[EntityReference],
        _context: Context,
        _host_session: HostSession,
        success_callback: Callable[[int, bool], None],
        _error_callback: Callable[[int, BatchElementError], None],
    ) -> None:
        """Check if entities exist in AYON.

        Args:
            entity_refs (List[EntityReference]): The entity references
                to check.
            _context (Context): The context.
            _host_session (HostSession): The host session.
            success_callback (Callable[[int, bool], None]): The success
                callback.
            _error_callback (Callable[[int, BatchElementError], None]): The
                error callback.

        """
        identities = ayon_core_util.query_identity_for_entity_refs(
            [str(ref) for ref in entity_refs]
        )

        for idx, rep in enumerate(identities):
            if rep["entities"]:
                success_callback(idx, True)  # noqa: FBT003
            else:
                success_callback(idx, False)  # noqa: FBT003

    def resolve(  # noqa: PLR0913, PLR0917
        self,
        entity_references: list[EntityReference],
        trait_set: set[str],
        resolve_access: access.ResolveAccess,
        context: Context,
        host_session: HostSession,
        success_callback: Callable[[int, TraitsData], Any],
        error_callback: Callable[[int, BatchElementError], Any],
    ) -> None:
        """Resolve entities in AYON.

        Args:
            entity_references (List[EntityReference]): The entity references
            trait_set (Set[str]): The trait set to resolve.
            resolve_access (access.ResolveAccess): The access mode.
            context (Context): The context.
            host_session (HostSession): The host session.
            success_callback (Callable[[int, TraitsData], Any]): The success
                callback.
            error_callback (Callable[[int, BatchElementError], Any]): The error
                callback.

        Raises:
            NotImplementedError: If the access mode is not supported.

        """
        if resolve_access == access.ResolveAccess.kRead:
            self.__resolve_for_read(
                entity_references,
                trait_set,
                context,
                host_session,
                success_callback,
                error_callback,
            )
        elif resolve_access == access.ResolveAccess.kManagerDriven:
            self.__resolve_for_manager_driven(
                entity_references,
                trait_set,
                context,
                host_session,
                success_callback,
                error_callback,
            )
        else:
            msg = f"Unexpected resolve() access mode: '{resolve_access}'"
            raise NotImplementedError(msg)

    def __resolve_for_read(  # noqa: C901, PLR0912, PLR0913, PLR0915, PLR0917
        self,
        entity_references: list[EntityReference],
        trait_set: set[str],
        context: Context,
        host_session: HostSession,
        success_callback: Callable[[int, TraitsData], Any],
        error_callback: Callable[[int, BatchElementError], Any],
    ) -> None:
        """Resolve entities for read access.

        Args:
            entity_references (List[EntityReference]): The entity references
                to resolve.
            trait_set (Set[str]): The trait set to resolve.
            context (Context): The context.
            host_session (HostSession): The host session.
            success_callback (Callable[[int, TraitsData], Any]): The success
                callback.
            error_callback (Callable[[int, BatchElementError], Any]): The error
                callback.

        Todo:
            - Refactor to reduce complexity.

        """
        # Cache results for a few seconds to avoid hammering the AYON server
        cache_key = (tuple(str(ref) for ref in entity_references), frozenset(trait_set))

        with self.__resolve_cache_lock:
            cached_value = self.__resolve_cache.get(cache_key)

        if cached_value is not None:
            for idx, traits_data in enumerate(cached_value):
                success_callback(idx, traits_data)
            return

        cached_value = [None] * len(entity_references)

        entity_identities = ayon_core_util.query_identity_for_entity_refs(
            [str(ref) for ref in entity_references]
        )

        for idx, rep in enumerate(entity_identities):
            entities = rep.get("entities")
            # If there are no entities in response, we were not able to resolve
            # any of the fields in the reference.
            if not entities:
                error_callback(
                    idx,
                    BatchElementError(
                        BatchElementError.ErrorCode.kEntityResolutionError,
                        "Entity not found"
                    ),
                )
                continue

            entity_identity = entities[-1]

            entity_info = ayon.parse_entity_ref(str(entity_references[idx]))

            entity_representation: Optional[dict[str, Any]] = None
            if representation_id := entity_identity.get("representationId"):
                entity_representation = ayon_api.get_representation_by_id(
                    entity_info.project_name, representation_id
                )

            entity_version: Optional[dict[str, dict]] = None
            if version_id := entity_identity.get("versionId"):
                entity_version = ayon_api.get_version_by_id(
                    entity_info.project_name, version_id)

            traits_data = TraitsData()

            # Display name:

            if mc_traits.identity.DisplayNameTrait.kId in trait_set:
                display_name_trait = mc_traits.identity.DisplayNameTrait(
                    traits_data)
                leaf_name = ""
                if entity_info.product_name:
                    leaf_name += entity_info.product_name
                if entity_info.task_name:
                    leaf_name += entity_info.task_name
                if entity_info.workfile_name:
                    leaf_name += f"/{entity_info.workfile_name}"
                if entity_info.version_name:
                    leaf_name += f"@{entity_info.version_name}"

                display_name_trait.setName(leaf_name)
                display_name_trait.setQualifiedName(
                    f"{entity_info.project_name}/{entity_info.path}/{leaf_name}"
                )

            # File path:

            if mc_traits.content.LocatableContentTrait.kId in trait_set:
                resolved_uri = None

                # Check if the representation is actually a collection
                # of files. If so, assume this means an ordered list of frames.
                # TODO(DF): Is this assumption correct?
                if entity_representation is not None and len(
                        entity_representation["files"]) > 1:
                    frame_token = self.__create_frame_token(
                        entity_info.project_name, host_session)

                    entity_representation["context"]["frame"] = frame_token

                    # Get root path for current project and site.
                    # TODO(DF): Cache this.
                    project_root = ayon_api.get_project_roots_by_site_id(
                        entity_info.project_name, ayon_api.get_site_id()
                    )

                    wildcard_path = pipeline.get_representation_path(
                        entity_representation, root=project_root
                    )

                    resolved_uri = Path(wildcard_path).as_uri()
                    mc_traits.content.LocatableContentTrait(traits_data).setIsTemplated(True)

                elif file_path := entity_identity.get("filePath"):
                    try:
                        resolved_uri = Path(file_path).as_uri()
                    except ValueError as exc:
                        # E.g. root path not resolved.
                        host_session.logger().error(
                            f"Failed to convert file path '{file_path}' "
                            f"to a URL: {exc}"
                        )
                    mc_traits.content.LocatableContentTrait(traits_data).setIsTemplated(False)

                if resolved_uri is not None:
                    # Only set location if URI was found. Note that if
                    # we don't set the location, the trait will not be
                    # imbued at all.
                    mc_traits.content.LocatableContentTrait(traits_data).setLocation(resolved_uri)

            if entity_version is not None:
                # Frame range:

                if mc_traits.timeDomain.FrameRangedTrait.kId in trait_set:
                    frame_start: int = cast(
                        "int", entity_version["attrib"].get("frameStart"))
                    frame_end: int = cast(
                        "int", entity_version["attrib"].get("frameEnd"))
                    handle_start: int = entity_version["attrib"].get(
                            "handleStart", 0)
                    handle_end: int = entity_version["attrib"].get(
                            "handleEnd", 0)
                    fps = entity_version["attrib"].get("fps")

                    if frame_start is not None and frame_end is not None:
                        frame_ranged_trait = mc_traits.timeDomain.FrameRangedTrait(traits_data)
                        frame_ranged_trait.setInFrame(frame_start)
                        frame_ranged_trait.setOutFrame(frame_end)
                        frame_ranged_trait.setStartFrame(frame_start - handle_start)
                        frame_ranged_trait.setEndFrame(frame_end + handle_end)

                        if fps is not None:
                            frame_ranged_trait.setFramesPerSecond(fps)

                # Colour space
                if mc_traits.color.OCIOColorManagedTrait.kId in trait_set:
                    if colorspace := entity_version["attrib"].get("colorSpace"):
                        ocio_trait = mc_traits.color.OCIOColorManagedTrait(
                            traits_data)
                        ocio_trait.setColorspace(colorspace)

            if traits_data.traitSet():
                # Add entity info to context for use in UI pre-population, etc.
                context.managerState.most_recent_resolved_entity_info = entity_info  # noqa: E501

            cached_value[idx] = traits_data
            success_callback(idx, traits_data)

        if all(v is not None for v in cached_value):
            with self.__resolve_cache_lock:
                self.__resolve_cache[cache_key] = cached_value

    def __resolve_for_manager_driven(
        self,
        entity_references: list[EntityReference],
        trait_set: set[str],
        _context: Context,
        host_session: HostSession,
        success_callback: Callable[[int, TraitsData], Any],
        error_callback: Callable[[int, BatchElementError], Any],
    ) -> None:
        """Resolve entities for manager-driven access."""
        entity_identities = ayon_core_util.query_identity_for_entity_refs(
            [str(ref) for ref in entity_references]
        )

        # Currently, we only support driving LocatableContentTrait
        # for publishing.
        if mc_traits.content.LocatableContentTrait.kId not in trait_set:
            for idx in range(len(entity_references)):
                success_callback(idx, TraitsData())
            return

        for idx, entity_ref in enumerate(entity_references):
            entity_info = ayon.parse_entity_ref(str(entity_ref))

            if entity_info.preflight_data is None:
                error_callback(
                    idx,
                    BatchElementError(
                        BatchElementError.ErrorCode.kMalformedEntityReference,
                        "Entity reference does not contain preflight "
                        "metadata. A working reference returned from "
                        "preflight() is required.",
                    ),
                )
                continue

            resolved_traits = TraitsData()

            if entity_info.workfile_name is not None:
                # In-progress workfile.
                resolved_path = ayon_core_util.query_workfile_path(
                    entity_info, entity_identities[idx]["entities"][-1]
                )
                if resolved_path is None:
                    success_callback(idx, TraitsData())
                    continue
                resolved_path = pathlib.Path(resolved_path)

            else:
                # Asset (i.e. representation).
                staging_dir_result = (
                    ayon_core_util.query_staging_dir_for_entity(entity_info)
                )
                staging_dir = (
                    staging_dir_result
                    if staging_dir_result is not None else None
                )
                if staging_dir is None:
                    success_callback(idx, TraitsData())
                    continue

                resolved_path = pathlib.Path(staging_dir)

                resolved_filename = entity_info.product_name
                if resolved_filename is None:
                    error_callback(idx, TraitsData())
                    return

                if entity_info.preflight_data.get("frame_ranged"):
                    frame_token = self.__create_frame_token(
                        entity_info.project_name, host_session)
                    resolved_filename += f".{frame_token}"

                # Append colour space to file name, as is OCIO convention,
                # and required for e.g. Katana.
                if colorspace := entity_info.preflight_data.get("colorspace"):
                    resolved_filename += f".{colorspace}"

                resolved_filename += f".{entity_info.representation_name}"

                resolved_path /= resolved_filename

            # TODO(DF): Can/should ayon_core do this for us?
            resolved_path.parent.mkdir(parents=True, exist_ok=True)

            mc_traits.content.LocatableContentTrait(resolved_traits).setLocation(
                resolved_path.as_uri()
            )

            success_callback(idx, resolved_traits)

    @staticmethod
    def preflight(
        target_entity_refs: list[EntityReference],
        traits_hints: list[TraitsData],
        _publishing_access: access.PublishingAccess,
        _context: Context,
        _host_session: HostSession,
        success_callback: Callable[[int, EntityReference], Any],
        _error_callback: Callable[[int, BatchElementError], Any],
    ) -> None:
        """Preflight entities for publishing in AYON.

        Args:
            target_entity_refs (List[EntityReference]): The entity references
                to preflight.
            traits_hints (List[TraitsData]): The traits data for each entity
                reference.
            _publishing_access (access.PublishingAccess): The publishing
                access mode.
            _context (Context): The context.
            _host_session (HostSession): The host session.
            success_callback (Callable[[int, EntityReference], Any]): The
                success callback.
            _error_callback (Callable[[int, BatchElementError], Any]): The
                error callback.

        """
        for idx, (entity_ref, entity_traits_data) in enumerate(
            zip(target_entity_refs, traits_hints)
        ):
            entity_info = ayon.parse_entity_ref(str(entity_ref))

            # Data to be encoded in the entity reference, for use elsewhere
            # (e.g. resolve() with kManagerDriven access).
            preflight_data = {}

            if entity_info.product_type is None:
                entity_info.product_type = "workfile"

            # If there's no workfile name, then assume a representation, and if
            # there's no product name, derive product name from the available
            # entity fields.
            if (
                    entity_info.workfile_name is None
                    and entity_info.product_name is None
            ):
                entity_info.product_name = (
                    ayon_core_util.query_product_name_for_entity(
                        entity_info
                    )
                )

            if mc_traits.timeDomain.FrameRangedTrait.isImbuedTo(
                    entity_traits_data):
                preflight_data["frame_ranged"] = True

            ocio_color_managed_trait = mc_traits.color.OCIOColorManagedTrait(
                entity_traits_data)
            if colorspace := ocio_color_managed_trait.getColorspace():
                preflight_data["colorspace"] = colorspace

            entity_info.preflight_data = preflight_data
            entity_info.version_name = None

            preflighted_ref_str = ayon.build_entity_ref(entity_info)
            preflighted_ref = EntityReference(preflighted_ref_str)

            success_callback(idx, preflighted_ref)

    def register(  # noqa: PLR0912, PLR0914, PLR0915, C901
        self,
        target_entity_refs: list[EntityReference],
        entity_traits_datas: list[TraitsData],
        _publishing_access: access.PublishingAccess,
        _context: Context,
        host_session: HostSession,
        success_callback: Callable[[int, EntityReference], Any],
        error_callback: Callable[[int, BatchElementError], Any],
    ) -> None:
        """Register (publish) entities in AYON.

        TODO (antirotor): Refactor to reduce complexity. This will be easier
            once AYON Core has more robust publishing API.

        Args:
            target_entity_refs (List[EntityReference]): The entity references
                to register.
            entity_traits_datas (List[TraitsData]): The traits data for each
                entity reference.
            _publishing_access (access.PublishingAccess): The publishing
                access mode.
            _context (Context): The context.
            host_session (HostSession): The host session.
            success_callback (Callable[[int, EntityReference], Any]): The
                success callback.
            error_callback (Callable[[int, BatchElementError], Any]): The
                error callback.

        """
        entity_identities = ayon_core_util.query_identity_for_entity_refs(
            [str(ref) for ref in target_entity_refs]
        )

        for idx, (entity_ref, entity_traits_data) in enumerate(
            zip(target_entity_refs, entity_traits_datas, strict=True)
        ):
            entity_info = ayon.parse_entity_ref(str(entity_ref))
            is_workfile = entity_info.representation_name is None
            instance_data = {}

            # Frame range metadata.
            frame_ranged_trait = mc_traits.timeDomain.FrameRangedTrait(
                entity_traits_data)
            if frame_ranged_trait.isImbued():
                start_frame = frame_ranged_trait.getStartFrame()
                if start_frame is not None:
                    instance_data["frameStart"] = start_frame
                    if in_frame := frame_ranged_trait.getInFrame():
                        instance_data["handleStart"] = in_frame - start_frame
                    else:
                        instance_data["handleStart"] = 0

                end_frame = frame_ranged_trait.getEndFrame()
                if end_frame is not None:
                    instance_data["frameEnd"] = end_frame
                    if out_frame := frame_ranged_trait.getOutFrame():
                        instance_data["handleEnd"] = end_frame - out_frame
                    else:
                        instance_data["handleEnd"] = 0

            # Colour space metadata.
            ocio_color_managed_trait = mc_traits.color.OCIOColorManagedTrait(
                entity_traits_data)
            if colorspace := ocio_color_managed_trait.getColorspace():
                instance_data["colorspace"] = colorspace

            # Find all the files.
            multiple_files: list[str] = []
            single_file: str | None = None
            if url := mc_traits.content.LocatableContentTrait(
                    entity_traits_data).getLocation():
                # Path to file, or template for a sequence of files.
                path = pathlib.Path(
                    self.__fileUrlPathConverter.pathFromUrl(url)
                )
                if is_workfile:
                    single_file = str(path)
                else:
                    # AYON uses "stagingDir" to mean the parent directory that
                    # all the files were written to. This could be the staging
                    # directory supplied by AYON itself (retrieved using
                    # resolve() with a kManagerDriven access mode), but not
                    # necessarily - it's just the directory where the output
                    # artifacts can be found.
                    instance_data["stagingDir"] = str(path.parent)
                    if not frame_ranged_trait.isImbued():
                        single_file = path.name
                    else:
                        # TODO(DF): We assume that the frame token is
                        #  in the file name(s), rather than in a directory
                        #  name.
                        frame_token = self.__create_frame_token(
                            entity_info.project_name, host_session
                        )
                        start_frame = frame_ranged_trait.getStartFrame()
                        end_frame = frame_ranged_trait.getEndFrame()

                        if frame_token not in path.name:
                            # Assume a single file, i.e. not a file
                            # sequence. In particular, a video file may
                            # have a frame range, but is only a single
                            # file.
                            single_file = path.name

                        elif start_frame is None or end_frame is None:
                            # No frame range is specified, so try a glob of the
                            # file system.
                            glob_path = path.with_name(
                                path.name.replace(frame_token, "*")
                            )
                            multiple_files.extend(
                                f.name
                                for f in glob_path.parent.glob(glob_path.name)
                            )

                        else:
                            # Frame range is specified, so we trust that it's
                            # accurate, and generate a list of files.
                            frame_padding = self.__query_frame_padding(
                                entity_info.project_name
                            )
                            frame_range = range(start_frame, end_frame + 1)

                            multiple_files.extend(
                                path.name.replace(
                                    frame_token, f"{frame:0{frame_padding}}"
                                )
                                for frame in frame_range
                            )

            if len(multiple_files) == 1:
                # AYON will complain if we try to pass a "sequence" containing
                # a single file. We flag that it's not a sequence by passing a
                # single string.
                single_file = multiple_files.pop()

            if not is_workfile:
                if not single_file and not multiple_files:
                    host_session.logger().error(
                        "No files found to publish representation for entity"
                        f" reference '{entity_ref}'"
                    )
                    error_callback(
                        idx,
                        BatchElementError(
                            BatchElementError.ErrorCode.kInvalidPreflightHint,
                            "No files found to publish representation.",
                        ),
                    )
                    continue

                # Execute the AYON Pyblish process.
                published_ids = ayon_core_util.publish_representation(
                    entity_info,
                    instance_data,
                    single_file or multiple_files,
                    host_session.logger(),
                )
                # Convert IDs to AYON URIs (i.e. entity references).
                uri_response = ayon_api.post(
                    f"projects/{entity_info.project_name}/uris",
                    entityType="representation",
                    ids=published_ids,
                )
                final_entity_ref_str = uri_response.data["uris"][-1]["uri"]
            else:
                if not single_file:
                    host_session.logger().error(
                        "No files found to publish workfile for entity"
                        f" reference '{entity_ref}'"
                    )
                    error_callback(
                        idx,
                        BatchElementError(
                            BatchElementError.ErrorCode.kInvalidPreflightHint,
                            "No files found to publish workfile.",
                        ),
                    )
                    continue

                # Assume a (in-progress) workfile
                ayon_core_util.publish_workfile(
                    entity_info,
                    entity_identities[idx]["entities"][-1]["taskId"],
                    single_file
                )

                entity_info.workfile_name = pathlib.Path(single_file).name
                entity_info.representation_name = None
                entity_info.product_name = None
                entity_info.product_type = None
                entity_info.version_name = None
                entity_info.preflight_data = None
                final_entity_ref_str = ayon.build_entity_ref(entity_info)

            success_callback(idx, EntityReference(final_entity_ref_str))

    def getWithRelationship(  # noqa: N802, PLR0913, PLR0917
        self,
        entity_references: list[EntityReference],
        relationship_traits_data: TraitsData,
        result_trait_set: set[str],  # noqa: ARG002
        page_size: int,
        relations_access: access.RelationsAccess,
        _context: Context,
        _host_session: HostSession,
        success_callback: Callable[[int, EntityReferencePagerInterface], Any],
        error_callback: Callable[[int, BatchElementError], Any],
    ) -> None:
        """Get entities related to the given entity references.

        Currently only supports the VersionTrait relationship with kRead
        access. For each input representation reference, returns a pager
        of references to versions of the same representation.

        The VersionTrait's ``specifiedTag`` and ``stableTag`` properties
        are honoured as optional filter predicates:

        - ``specifiedTag`` only supports the value ``"latest"`` (in which
          case only the most recent version is returned). Any other value
          yields an empty pager.
        - ``stableTag`` filters to a single concrete version number; both
          ``"vNNN"`` and ``"N"`` formats are accepted. Any other value
          yields an empty pager.

        Both filters may be supplied at the same time, in which case both
        must match (AND).

        The ``result_trait_set`` parameter is currently ignored.

        Args:
            entity_references (list[EntityReference]): The entity
                references for which to look up related entities.
            relationship_traits_data (TraitsData): The traits data
                describing the relationship to query.
            result_trait_set (set[str]): The trait set of the related
                entities (currently ignored).
            page_size (int): The page size for the returned pager.
            relations_access (access.RelationsAccess): The access mode
                for the relationship query.
            _context (Context): The context.
            _host_session (HostSession): The host session.
            success_callback (Callable[[int, EntityReferencePagerInterface],
                Any]): The success callback.
            error_callback (Callable[[int, BatchElementError], Any]):
                The error callback.

        """
        if not self.__validate_relationship_access(
            relations_access, len(entity_references), error_callback
        ):
            return

        if not self.__is_version_relationship(relationship_traits_data):
            # Unsupported relationship - return empty pagers.
            for idx in range(len(entity_references)):
                success_callback(
                    idx, _AyonEntityReferencePagerInterface(page_size, [])
                )
            return

        version_filter = self.__parse_version_filter(relationship_traits_data)
        if version_filter is None:
            # Filter excludes everything.
            for idx in range(len(entity_references)):
                success_callback(
                    idx, _AyonEntityReferencePagerInterface(page_size, [])
                )
            return

        entity_identities = ayon_core_util.query_identity_for_entity_refs(
            [str(ref) for ref in entity_references]
        )

        for idx, entity_ref in enumerate(entity_references):
            self.__handle_version_relationship(
                idx,
                entity_ref,
                entity_identities[idx],
                page_size,
                version_filter,
                success_callback,
                error_callback,
            )

    def getWithRelationships(  # noqa: N802, PLR0913, PLR0917
        self,
        entity_reference: EntityReference,
        relationship_traits_datas: list[TraitsData],
        result_trait_set: set[str],  # noqa: ARG002
        page_size: int,
        relations_access: access.RelationsAccess,
        _context: Context,
        _host_session: HostSession,
        success_callback: Callable[[int, EntityReferencePagerInterface], Any],
        error_callback: Callable[[int, BatchElementError], Any],
    ) -> None:
        """Get entities related to a single entity reference.

        Currently only supports the VersionTrait relationship with kRead
        access. For each relationship in the batch, returns a pager of
        references to versions of the same representation referred to
        by ``entity_reference``.

        See ``getWithRelationship`` for details of the supported
        VersionTrait predicate properties (``specifiedTag`` and
        ``stableTag``).

        The ``result_trait_set`` parameter is currently ignored.

        Args:
            entity_reference (EntityReference): The entity reference for
                which to look up related entities.
            relationship_traits_datas (list[TraitsData]): The traits data
                for each relationship to query.
            result_trait_set (set[str]): The trait set of the related
                entities (currently ignored).
            page_size (int): The page size for the returned pager.
            relations_access (access.RelationsAccess): The access mode
                for the relationship query.
            _context (Context): The context.
            _host_session (HostSession): The host session.
            success_callback (Callable[[int, EntityReferencePagerInterface],
                Any]): The success callback.
            error_callback (Callable[[int, BatchElementError], Any]):
                The error callback.

        """
        if not self.__validate_relationship_access(
            relations_access, len(relationship_traits_datas), error_callback
        ):
            return

        # Resolve the identity of the input reference once.
        entity_identity = ayon_core_util.query_identity_for_entity_refs(
            [str(entity_reference)]
        )[0]

        for idx, relationship_traits_data in enumerate(
                relationship_traits_datas):
            if not self.__is_version_relationship(relationship_traits_data):
                success_callback(
                    idx, _AyonEntityReferencePagerInterface(page_size, [])
                )
                continue

            version_filter = self.__parse_version_filter(
                relationship_traits_data)
            if version_filter is None:
                success_callback(
                    idx, _AyonEntityReferencePagerInterface(page_size, [])
                )
                continue

            self.__handle_version_relationship(
                idx,
                entity_reference,
                entity_identity,
                page_size,
                version_filter,
                success_callback,
                error_callback,
            )

    @staticmethod
    def __is_version_relationship(
            relationship_traits_data: TraitsData) -> bool:
        """Whether the given relationship data is a Version query.

        We support the Version trait being present in the relationship
        trait set, regardless of any other traits.
        """
        return mc_traits.lifecycle.VersionTrait.isImbuedTo(
            relationship_traits_data)

    @staticmethod
    def __parse_version_filter(
        relationship_traits_data: TraitsData,
    ) -> Callable[[int, int], bool] | None:
        """Build a predicate from the VersionTrait filter properties.

        Returns a callable ``(version_num, latest_version_num) -> bool``
        that decides whether a given version number should be included
        in the results, or ``None`` if the filter cannot be satisfied by
        any version (in which case the caller should return an empty
        pager).

        Supported properties:

        - ``specifiedTag``: only ``"latest"`` is supported. Any other
            value returns ``None``.
        - ``stableTag``: a concrete version number, either as ``"vNNN"``
            or ``"N"``. Any other value (including ``"latest"``) returns
            ``None``.
        """
        version_trait = mc_traits.lifecycle.VersionTrait(
            relationship_traits_data)

        specified_tag = version_trait.getSpecifiedTag()
        stable_tag = version_trait.getStableTag()

        only_latest = False
        if specified_tag is not None:
            if specified_tag != "latest":
                return None
            only_latest = True

        stable_version_num: int | None = None
        if stable_tag is not None:
            try:
                stable_version_num = int(stable_tag.lstrip("vV"))
            except ValueError:
                return None

        def predicate(version_num: int, latest_version_num: int) -> bool:
            if only_latest and version_num != latest_version_num:
                return False
            if (
                stable_version_num is not None
                and version_num != stable_version_num
            ):
                return False
            return True

        return predicate

    @staticmethod
    def __validate_relationship_access(
        relations_access: access.RelationsAccess,
        batch_size: int,
        error_callback: Callable[[int, BatchElementError], Any],
    ) -> bool:
        """Validate the access mode for a relationship query.

        Currently only kRead access is supported.
        """
        if relations_access == access.RelationsAccess.kRead:
            return True
        for idx in range(batch_size):
            error_callback(
                idx,
                BatchElementError(
                    BatchElementError.ErrorCode.kEntityAccessError,
                    "Only kRead access is supported for relationship queries",
                ),
            )
        return False

    @staticmethod
    def __handle_version_relationship(  # noqa: PLR0913, PLR0917
        idx: int,
        entity_ref: EntityReference,
        entity_identity: dict,
        page_size: int,
        version_filter: Callable[[int, int], bool],
        success_callback: Callable[[int, EntityReferencePagerInterface], Any],
        error_callback: Callable[[int, BatchElementError], Any],
    ) -> None:
        """Resolve and return matching versions of the given representation.

        Builds a pager of entity references corresponding to all versions
        of the representation pointed to by ``entity_ref`` that pass
        ``version_filter``. Reports ``kEntityResolutionError`` if the
        reference cannot be resolved, and ``kInvalidEntityReference`` if
        it does not point to a representation.
        """
        entities = entity_identity.get("entities") or []
        if not entities:
            error_callback(
                idx,
                BatchElementError(
                    BatchElementError.ErrorCode.kEntityResolutionError,
                    "Entity not found",
                ),
            )
            return

        entity = entities[-1]
        representation_id = entity.get("representationId")
        version_id = entity.get("versionId")
        if not representation_id or not version_id:
            error_callback(
                idx,
                BatchElementError(
                    BatchElementError.ErrorCode.kInvalidEntityReference,
                    "Version relationship queries are only supported for "
                    "representation entity references",
                ),
            )
            return

        entity_info = ayon.parse_entity_ref(str(entity_ref))

        if not entity_info.representation_name:
            error_callback(
                idx,
                BatchElementError(
                    BatchElementError.ErrorCode.kInvalidEntityReference,
                    "Entity reference must include a representation name",
                ),
            )
            return

        # Find the product (i.e. the logical entity) that owns this version.
        current_version = ayon_api.get_version_by_id(
            entity_info.project_name, version_id, fields=["productId"]
        )
        if current_version is None:
            error_callback(
                idx,
                BatchElementError(
                    BatchElementError.ErrorCode.kEntityResolutionError,
                    "Could not resolve product for entity",
                ),
            )
            return

        product_id = current_version["productId"]

        # Fetch all versions of the product and all representations of
        # those versions matching the input representation name.
        all_versions = list(
            ayon_api.get_versions(
                entity_info.project_name,
                product_ids=[product_id],
                fields=["id", "version"],
            )
        )
        version_num_by_id = {v["id"]: v["version"] for v in all_versions}
        version_ids = list(version_num_by_id.keys())

        all_reps = list(
            ayon_api.get_representations(
                entity_info.project_name,
                version_ids=version_ids,
                representation_names=[entity_info.representation_name],
                fields=["id", "versionId"],
            )
        ) if version_ids else []

        # Latest version - taken as the highest version number across
        # all returned versions (negative numbers, used by AYON for
        # hero versions, naturally lose to standard version numbers).
        latest_version_num = (
            max(version_num_by_id.values()) if version_num_by_id else 0
        )

        # Order results by version number (most recent first), so that the
        # input version typically appears near the top.
        all_reps.sort(
            key=lambda rep: version_num_by_id.get(rep["versionId"], 0),
            reverse=True,
        )

        related_refs: list[EntityReference] = []
        for rep in all_reps:
            version_num = version_num_by_id.get(rep["versionId"])
            if version_num is None:
                continue
            if not version_filter(version_num, latest_version_num):
                continue
            new_info = dataclasses.replace(
                entity_info,
                version_name=f"v{version_num:03d}",
                # Drop preflight/working metadata - these refs point at
                # already-published versions.
                preflight_data=None,
            )
            related_refs.append(
                EntityReference(ayon.build_entity_ref(new_info))
            )

        success_callback(
            idx, _AyonEntityReferencePagerInterface(page_size, related_refs)
        )

    @staticmethod
    def __query_frame_padding(project_name: str) -> int:
        """Get the desired frame padding for the project.

        Args:
            project_name (str): The name of the project.

        Returns:
            int: The frame padding for the project.

        """
        project_anatomy_response = ayon_api.get(
            f"projects/{project_name}/anatomy")
        return project_anatomy_response["templates"]["frame_padding"]

    @classmethod
    def __create_frame_token(
            cls, project_name: str, host_session: HostSession) -> str:
        """Construct a frame number wildcard.

        The wildcard should be `{frame}` by default, to satisfy the OpenAssetIO
        standard. But for compatibility, we should check the host application,
        and use the most appropriate token.

        Args:
            project_name (str): The name of the project.
            host_session (HostSession): The host session.

        Returns:
            str: The frame number wildcard.

        """
        frame_padding = cls.__query_frame_padding(project_name)
        if host_session.host().identifier().startswith("com.foundry"):
            # Nuke/Katana use `#`s (can also use e.g. %04d).
            return "#" * frame_padding

        # The "OpenAssetIO standard" (subset of fmtlib/Python format strings).
        return f"{{frame:0{frame_padding}}}"


class _AyonEntityReferencePagerInterface(EntityReferencePagerInterface):
    """Simple in-memory pager.

    All entity references are queried up-front, then split into pages
    of the requested size, ready to be returned on demand.
    """

    def __init__(
            self, page_size: int, entity_references: list[EntityReference]):
        """Constructor.

        Args:
            page_size (int): The desired page size. Must be > 0.
            entity_references (list[EntityReference]): The full list of
                entity references to paginate.

        """
        EntityReferencePagerInterface.__init__(self)
        self.__page_num = 0
        self.__pages: list[list[EntityReference]] = []
        if page_size <= 0:
            page_size = max(1, len(entity_references))
        for page_start in range(0, len(entity_references), page_size):
            self.__pages.append(
                entity_references[page_start:page_start + page_size]
            )

    def close(self, _host_session: HostSession) -> None:  # noqa: D102
        # Nothing to clean up.
        self.__pages = []

    def hasNext(self, _host_session: HostSession) -> bool:  # noqa: N802, D102
        return self.__page_num < len(self.__pages) - 1

    def next(self, _host_session: HostSession) -> None:  # noqa: D102
        if self.__page_num < len(self.__pages):
            self.__page_num += 1

    def get(self, _host_session: HostSession) -> list[EntityReference]:  # noqa: D102
        if self.__page_num >= len(self.__pages):
            return []
        return list(self.__pages[self.__page_num])

