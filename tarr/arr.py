import json
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import structlog
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr

from tarr.config import ArrConfig

log = structlog.stdlib.get_logger("tarr")


class QueueRecord(BaseModel):
    model_config = ConfigDict(strict=True)

    downloadId: StrictStr | None = None


class QueuePage(BaseModel):
    model_config = ConfigDict(strict=True)

    page: StrictInt = Field(ge=1)
    pageSize: StrictInt = Field(ge=1)
    totalRecords: StrictInt = Field(ge=0)
    records: list[QueueRecord]


def torrent_in_queue(cfg: ArrConfig, torrent_hash: str) -> bool:
    page = 1
    seen = 0
    while True:
        query = urlencode(
            {
                "page": page,
                "pageSize": 100,
                "sortKey": "id",
                "sortDirection": "ascending",
                "includeUnknownMovieItems": "true",
                "includeUnknownSeriesItems": "true",
            }
        )
        request = Request(
            f"{cfg.host}/api/v3/queue?{query}",
            headers={"X-Api-Key": cfg.api_key, "Accept": "application/json"},
        )
        with urlopen(request, timeout=10) as response:
            queue = QueuePage.model_validate(json.load(response))
        if queue.page != page or len(queue.records) > queue.pageSize:
            raise ValueError("invalid Arr queue pagination")
        if any(
            record.downloadId is not None and record.downloadId.lower() == torrent_hash.lower()
            for record in queue.records
        ):
            return True
        seen += len(queue.records)
        if seen >= queue.totalRecords:
            return False
        if len(queue.records) != queue.pageSize:
            raise ValueError("incomplete Arr queue page")
        page += 1


def removal_allowed(
    torrent_hash: str,
    instances: list[str],
    arrs: dict[str, ArrConfig],
) -> bool:
    for instance in instances:
        try:
            if torrent_in_queue(arrs[instance], torrent_hash):
                log.info("torrent remains in Arr queue; skipping removal", arr=instance)
                return False
        except Exception as error:
            log.warning(
                "unable to verify Arr queue; skipping removal",
                arr=instance,
                error_type=type(error).__name__,
            )
            return False
    return True
