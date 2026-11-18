"""读取锁定公开发行源码及包随附文档，并记录可核验的来源。"""

from __future__ import annotations

from dataclasses import dataclass
from importlib.metadata import distribution
from pathlib import Path


HERE = Path(__file__).resolve().parent  # 本专项评测脚本目录。
EARLIER = HERE.parent / "memory-repository-20261117" / "sources"  # 先前验真的原生发行归档。


@dataclass(frozen=True)
class Source:
    """一个评测仓库的公开原件、来源及候选导入根目录。"""

    slug: str  # 冻结题单引用的短标识。
    project: str  # 上游仓库所有者和名称。
    version: str  # 本次固定版本；不同版本不得代换。
    package: str  # 源码包目录，与发行包名称可以不同。
    native_root: Path | None = None  # 本地已有发行归档；为 None 时读指定版本的已安装发行原件。
    distribution_name: str | None = None  # 元数据中的发行包名称，归档源不需要此值。

    @property
    def import_root(self) -> str:
        """返回候选内的导入根，已有原生 src 结构时保留该前缀。"""
        return "src" if self.native_root is None or (self.native_root / "src").is_dir() else "."

    @property
    def release_url(self) -> str:
        """返回上游项目的固定标签或包的固定发行页面。"""
        if self.native_root is None:
            return f"https://pypi.org/project/{self.distribution_name}/{self.version}/"
        tag = f"v{self.version}" if self.slug == "settings" else self.version
        return f"https://github.com/{self.project}/tree/{tag}"

    def module_path(self, name: str) -> str:
        """由无 .py 后缀的包内相对模块名 name 返回冻结文件路径。"""
        relative = Path(self.package, *name.split(".")) if name else Path(self.package, "__init__")
        return (Path(self.import_root) / relative.with_suffix(".py")).as_posix()


SOURCES = (
    Source("settings", "pydantic/pydantic-settings", "2.16.3", "pydantic_settings",
           EARLIER / "config" / "pydantic-settings-2.16.3"),
    Source("marsh", "marshmallow-code/marshmallow", "4.3.1", "marshmallow", EARLIER / "importer"),
    Source("jinja", "pallets/jinja", "3.1.6", "jinja2", EARLIER / "reporting"),
    Source("click", "pallets/click", "8.5.0", "click", distribution_name="click"),
    Source("packaging", "pypa/packaging", "26.3", "packaging", distribution_name="packaging"),
    Source("pluggy", "pytest-dev/pluggy", "1.7.0", "pluggy", distribution_name="pluggy"),
)  # 本批 6 个预先登记的仓库，不从其他评测的工作区取资料。


def _text(path: Path) -> str | None:
    """以 UTF-8 读取原件 path；二进制文件返回 None，失效来源抛出异常。"""
    raw = path.read_bytes()
    if b"\x00" in raw:
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _candidate_files(source: Source) -> tuple[list[tuple[str, Path]], str]:
    """返回 source 中可供冻结的路径映射及来源种类，保持原生层级。"""
    native = source.native_root
    if native is not None:
        return [(p.relative_to(native).as_posix(), p) for p in native.rglob("*") if p.is_file()], "source archive"
    assert source.distribution_name is not None
    dist = distribution(source.distribution_name)
    if dist.version != source.version:
        raise ValueError(f"{source.slug} 发行版不符合固定版本")
    paths = []
    for item in dist.files or ():
        parts = tuple(item.parts)
        if parts[0] == source.package:
            paths.append(((Path("src") / Path(*parts)).as_posix(), Path(dist.locate_file(item))))
        elif parts[0].endswith(".dist-info") and (parts[-1] == "METADATA" or "licenses" in parts):
            paths.append(((Path("distribution") / Path(*parts[1:])).as_posix(), Path(dist.locate_file(item))))
    return paths, "installed upstream distribution"


def read_source(source: Source) -> tuple[dict[str, str], dict]:
    """读取 source 固定公开原件，返回 UTF-8 文件与来源清单；排除本机缓存和安装记录。"""
    texts, omitted = {}, []
    candidates, kind = _candidate_files(source)
    for rel, path in sorted(candidates):
        if set(Path(rel).parts) & {".git", "__pycache__", ".venv", ".pytest_cache"}:
            continue
        if path.is_symlink() or path.is_junction() or not path.is_file():
            raise ValueError(f"上游原件不是普通文件 {rel}")
        value = _text(path)
        if value is None:
            omitted.append(rel)
        else:
            texts[rel] = value
    docs = [n for n in texts if Path(n).suffix.lower() in {".md", ".txt", ".rst"} or n.endswith("/METADATA")]
    if not docs or not texts.get(source.module_path("")):
        raise ValueError(f"公开快照缺少包入口或出版资料 {source.slug}")
    return texts, {"repository": source.project, "version": source.version, "url": source.release_url,
                   "origin": kind, "utf8_files": len(texts), "published_documents": len(docs),
                   "omitted_binary": omitted}
