# Copyright (c) 2026 OceanBase.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""SeekDB native single-statement hybrid candidate retrieval.

The two candidate channels are produced by one database statement: a FULLTEXT
branch over the active-head projection and a vector-index branch over the
rebuildable embedding projection.  Each branch applies the scope, Memory
artifact, tag and candidate-limit filters in SQL, so the application only
fuses the two already bounded rankings.
"""

from __future__ import annotations

from pyobvector import VECTOR
from sqlalchemy import bindparam, text
from sqlalchemy.ext.asyncio import AsyncConnection

from powercontext.builtin.artifacts.memory.canonical import validate_embedding
from powercontext.builtin.artifacts.memory.errors import CapabilityNotSupportedError
from powercontext.builtin.artifacts.memory.models import EmbeddingProfile
from powercontext.builtin.artifacts.memory.protocols import MemorySearchChannels, MemorySearchRequest
from powercontext.builtin.persistence.tables import MEMORY_ENTRY_HEADS_TABLE
from powercontext.builtin.persistence.tags import memory_tag_parameters, memory_tag_sql

# Mirrors OceanBaseMemoryVectorIndex's projection table (the schema is shared by
# the OceanBase and SeekDB relational profiles).
_VECTOR_TABLE_NAME = "pc_memory_vector_entries"


def _candidate_sql(*, tagged: bool) -> str:
    heads = MEMORY_ENTRY_HEADS_TABLE.name
    fts_tag = memory_tag_sql("h") if tagged else ""  # type: ignore[arg-type]
    vector_tag = memory_tag_sql("v") if tagged else ""  # type: ignore[arg-type]
    return f"""
SELECT candidates.memory_artifact_id AS memory_artifact_id,
       candidates.head_revision AS head_revision,
       candidates.entry_id AS entry_id,
       candidates.entry_version_id AS entry_version_id,
       candidates.text AS text,
       candidates.distance AS distance,
       candidates.channel AS channel
FROM (
  (SELECT h.memory_artifact_id AS memory_artifact_id,
          h.head_revision AS head_revision,
          h.entry_id AS entry_id,
          h.entry_version_id AS entry_version_id,
          e.text AS text,
          NULL AS distance,
          'fts' AS channel,
          MATCH(h.searchable_text) AGAINST (:query) AS relevance
   FROM {heads} AS h
   JOIN pc_memory_entry_versions AS e
     ON e.scope_id = h.scope_id
    AND e.memory_artifact_id = h.memory_artifact_id
    AND e.entry_id = h.entry_id
    AND e.entry_version_id = h.entry_version_id
   WHERE h.scope_id = :scope_id
     AND h.memory_artifact_id IN :memory_artifact_ids
     AND MATCH(h.searchable_text) AGAINST (:query){fts_tag}
   ORDER BY relevance DESC, h.memory_artifact_id, h.entry_id, h.entry_version_id
   LIMIT :candidate_limit)
  UNION ALL
  (SELECT v.memory_artifact_id AS memory_artifact_id,
          v.head_revision AS head_revision,
          v.entry_id AS entry_id,
          v.entry_version_id AS entry_version_id,
          e.text AS text,
          l2_distance(v.embedding, :query_vector) AS distance,
          'vector' AS channel,
          NULL AS relevance
   FROM {_VECTOR_TABLE_NAME} AS v
   JOIN pc_memory_entry_versions AS e
     ON e.scope_id = v.scope_id
    AND e.memory_artifact_id = v.memory_artifact_id
    AND e.entry_id = v.entry_id
    AND e.entry_version_id = v.entry_version_id
   WHERE v.scope_id = :scope_id
     AND v.memory_artifact_id IN :memory_artifact_ids{vector_tag}
   ORDER BY l2_distance(v.embedding, :query_vector) APPROXIMATE
   LIMIT :candidate_limit)
) AS candidates
ORDER BY candidates.channel ASC,
         candidates.relevance DESC,
         candidates.distance ASC,
         candidates.memory_artifact_id,
         candidates.entry_id,
         candidates.entry_version_id
""".strip()


def _statement(dimension: int, *, tagged: bool):
    bind = [
        bindparam("memory_artifact_ids", expanding=True),
        bindparam("query_vector", type_=VECTOR(dimension)),
    ]
    if tagged:
        bind.extend((bindparam("tag_keys", expanding=True), bindparam("tag_hashes", expanding=True)))
    return text(_candidate_sql(tagged=tagged)).bindparams(*bind)


async def search_native_hybrid(
    connection: AsyncConnection,
    scope_id: str,
    request: MemorySearchRequest,
    profile: EmbeddingProfile,
    /,
) -> MemorySearchChannels:
    """Run one native candidate statement and split its two ordered channels."""

    # Imported lazily: persistence.memory_index imports this module's dialect
    # probe, so keeping the row decoding import here breaks the import cycle.
    from powercontext.builtin.persistence.memory_index import memory_channel_hits

    if request.embedding_profile != profile or request.query_vector is None:
        raise CapabilityNotSupportedError("embedding-profile")

    tagged = request.tag_filter is not None
    parameters = {
        "query": request.analyzed_query,
        "scope_id": scope_id,
        "memory_artifact_ids": tuple(memory.artifact_id for memory in request.memories),
        "query_vector": validate_embedding(request.query_vector, dimension=profile.dimension),
        "candidate_limit": request.candidate_limit,
        **memory_tag_parameters(request.tag_filter),
    }
    rows = (await connection.execute(_statement(profile.dimension, tagged=tagged), parameters)).mappings()

    fts_rows: list[dict[str, object]] = []
    vector_rows: list[dict[str, object]] = []
    for row in rows:
        decoded = {
            "memory_artifact_id": row["memory_artifact_id"],
            "head_revision": row["head_revision"],
            "entry_id": row["entry_id"],
            "entry_version_id": row["entry_version_id"],
            "text": row["text"],
            "distance": row["distance"],
        }
        if str(row["channel"]) == "fts":
            decoded["distance"] = None
            fts_rows.append(decoded)
        else:
            vector_rows.append(decoded)
    return MemorySearchChannels(
        fts=memory_channel_hits(fts_rows, request.memories),
        vector=memory_channel_hits(vector_rows, request.memories),
    )
