# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""Seed and prune Maven SNAPSHOT metadata around a local-repository deploy.

``maven-deploy-plugin`` numbers a SNAPSHOT deploy from the
``maven-metadata.xml`` it finds in the target repository. A merge lane
deploys to a local file repository (the ``m2repo``) and publishes that
tree afterwards, so the deploy continues the published ``buildNumber``
sequence only when the published metadata is there first.

``fetch`` runs before the build. It asks Maven for the reactor's
coordinates, downloads the metadata files the deploy plugin reads,
writes them into the m2repo, and keeps a pristine copy (the baseline)
outside the workspace.

``prune`` runs after the build. It deletes every metadata file still
byte-identical to its baseline copy, with its checksum and signature
siblings. Those describe modules the build did not deploy; publishing
them would overwrite whatever a sibling build published meanwhile.

The package uses the standard library alone.
"""


class ActionError(Exception):
    """A failure to report to the caller, then exit non-zero."""
