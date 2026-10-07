"""Versioned, platform-tagged opened filesystem object identity."""

from dataclasses import dataclass


@dataclass(frozen=True)
class RootIdentity:
    kind: str
    volume: str
    file_id: str

    def as_json(self) -> dict:
        return dict(version=1, kind=self.kind, volume=self.volume, file_id=self.file_id)

    @classmethod
    def from_json(cls, value: dict):
        if (
            not isinstance(value, dict)
            or set(value) != {"version", "kind", "volume", "file_id"}
            or type(value["version"]) is not int
            or value["version"] != 1
            or any(type(value[key]) is not str for key in ("kind", "volume", "file_id"))
            or value["kind"] not in {"posix", "windows"}
        ):
            from agentforge.projects.errors import UnsafeProjectPath

            raise UnsafeProjectPath("Unsupported registered filesystem identity")
        return cls(value["kind"], value["volume"], value["file_id"])
