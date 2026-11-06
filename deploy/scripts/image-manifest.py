"""构建机使用：从 docker save 提取稳定 config digest，兼容不同 Docker 镜像存储后端。"""
import hashlib
import json
import sys
import tarfile
from pathlib import Path


def enrich(directory: Path) -> None:
    path = directory / "manifest.tsv"
    expected = [line.split("\t") for line in path.read_text().splitlines()]
    platform = (directory / "PLATFORM").read_text().strip()
    with tarfile.open(directory / "images.tar.gz", "r:gz") as archive:
        source = archive.extractfile("manifest.json")
        if source is None:
            raise ValueError("docker save archive lacks manifest.json")
        saved = json.load(source)
        configs = {}
        for entry in saved:
            stream = archive.extractfile(entry["Config"])
            if stream is None:
                raise ValueError("docker save archive lacks image config")
            raw = stream.read()
            config = json.loads(raw)
            if f"{config['os']}/{config['architecture']}" != platform:
                raise ValueError("image config architecture does not match bundle")
            identity = "sha256:" + hashlib.sha256(raw).hexdigest()
            for tag in entry.get("RepoTags") or []:
                if tag in configs:
                    raise ValueError("duplicate image tag in archive")
                configs[tag] = identity
        if set(configs) != {row[0] for row in expected}:
            raise ValueError("exported image tags do not match manifest")
        rows = []
        for image, identity, declared in expected:
            if declared != platform:
                raise ValueError("manifest platform mismatch")
            aliases = ",".join(dict.fromkeys([identity, configs[image]]))
            rows.append(f"{image}\t{aliases}\t{platform}\n")
    path.write_text("".join(rows))


if __name__ == "__main__":
    enrich(Path(sys.argv[1]))
