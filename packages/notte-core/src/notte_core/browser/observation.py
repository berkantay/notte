from base64 import b64encode
from typing import Annotated

from PIL import Image
from pydantic import BaseModel, Field

from notte_core.browser.snapshot import BrowserSnapshot, SnapshotMetadata
from notte_core.controller.space import BaseActionSpace
from notte_core.data.space import DataSpace
from notte_core.utils.url import clean_url


class TrajectoryProgress(BaseModel):
    current_step: int
    max_steps: int


class Observation(BaseModel):
    metadata: Annotated[
        SnapshotMetadata, Field(description="Metadata of the current page, i.e url, page title, snapshot timestamp.")
    ]
    screenshot: Annotated[
        bytes | None, Field(description="Base64 encoded screenshot of the current page", repr=False)
    ] = None
    space: BaseActionSpace | None = None
    data: Annotated[DataSpace | None, Field(description="Scraped data from the page")] = None
    progress: Annotated[
        TrajectoryProgress | None, Field(description="Progress of the current trajectory (i.e number of steps)")
    ] = None

    model_config = {  # type: ignore[reportUnknownMemberType]
        "json_encoders": {
            bytes: lambda v: b64encode(v).decode("utf-8") if v else None,
        }
    }

    @property
    def clean_url(self) -> str:
        return clean_url(self.metadata.url)

    def has_space(self) -> bool:
        return self.space is not None

    def has_data(self) -> bool:
        return self.data is not None

    def display_screenshot(self) -> "Image.Image | None":
        from notte_core.utils.image import image_from_bytes

        if self.screenshot is None:
            return None
        return image_from_bytes(self.screenshot)

    def valid_action_set(self) -> set[str]:
        valid_action_set: set[str] = set()
        if self.space is not None and self.space.actions:
            for action in self.space.actions("all"):
                valid_action_set.add(action.id)
        return valid_action_set
    
    
    @staticmethod
    def from_snapshot(
        snapshot: BrowserSnapshot,
        space: BaseActionSpace | None = None,
        data: DataSpace | None = None,
        progress: TrajectoryProgress | None = None,
    ) -> "Observation":
        return Observation(
            metadata=snapshot.metadata,
            screenshot=snapshot.screenshot,
            space=space,
            data=data,
            progress=progress,
        )
