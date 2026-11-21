# -*- coding: utf-8 -*-
"""把皮肤包里的 webp / 缩略图导入项目，并生成 static/skins/skins.json 清单。

用法：
    python import_skins.py "C:\\path\\to\\art\\incoming" "C:\\path\\to\\project\\static\\skins"

只读取源目录，只写入目标目录，不改动源文件。
"""
import json
import ntpath
import os
import shutil
import stat
import sys
import tempfile
from pathlib import Path, PureWindowsPath
from urllib.parse import quote


def reject_link(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(info.st_mode) or (
            getattr(info, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
        raise ValueError(f'不支持链接或重解析点：{path.name}')


def pack_path(root, name):
    if not isinstance(name, str):
        raise ValueError('素材路径必须为字符串')
    rel = PureWindowsPath(name)
    if rel.anchor or not rel.parts:
        raise ValueError('素材必须位于皮肤包内')
    path = root
    for part in rel.parts:
        reserved = ntpath.isreserved(part) if hasattr(ntpath, 'isreserved') else PureWindowsPath(part).is_reserved()
        if (part == '..' or reserved or part.endswith((' ', '.')) or
                any(c in '<>:"|?*' or ord(c) < 32 for c in part)):
            raise ValueError(f'不支持的素材路径：{name}')
        path = path / part
        reject_link(path)
    return path


def copy_image(source, target, thumbnail=False):
    with source.open('rb') as f:
        if os.fstat(f.fileno()).st_nlink != 1:
            raise ValueError(f'不支持硬链接素材：{source.name}')
        header = f.read(16)
        signatures = {
            '.webp': header[:4] == b'RIFF' and header[8:12] == b'WEBP'
                     and header[12:16] in (b'VP8 ', b'VP8L', b'VP8X'),
        }
        if thumbnail:
            signatures.update({
                '.png': header.startswith(b'\x89PNG\r\n\x1a\n'),
                '.jpg': header.startswith(b'\xff\xd8\xff'),
                '.jpeg': header.startswith(b'\xff\xd8\xff'),
                '.gif': header[:6] in (b'GIF87a', b'GIF89a'),
            })
        if not signatures.get(source.suffix.lower()):
            raise ValueError(f'不支持的图片格式或文件内容：{source.name}')
        with target.open('wb') as out:
            out.write(header)
            shutil.copyfileobj(f, out)


def stage_pack(src, stage, index):
    (stage / 'thumbs').mkdir()
    manifest = []
    missing = 0
    for role in index.get('roles', []):
        role_id = role.get('roleId')
        role_name = role.get('name') or role_id
        for skin in role.get('skins', []):
            web_rel = skin.get('webp')
            thumb_rel = skin.get('thumbnail')
            if not web_rel or not thumb_rel:
                continue
            web_src = pack_path(src, web_rel)
            thumb_src = pack_path(src, thumb_rel)
            if not (web_src.is_file() and thumb_src.is_file()):
                missing += 1
                continue

            copy_image(web_src, stage / web_src.name)
            copy_image(thumb_src, stage / 'thumbs' / thumb_src.name, thumbnail=True)

            manifest.append({
                'id': skin.get('skinId'),
                'name': skin.get('name') or '',
                'roleId': role_id,
                'roleName': role_name,
                'web': '/static/skins/' + quote(web_src.name),
                'thumb': '/static/skins/thumbs/' + quote(thumb_src.name),
                'mobilePosition': skin.get('mobileObjectPosition') or '50% 50%',
            })

    manifest.sort(key=lambda x: (x['roleId'], x['id']))
    out = {
        'count': len(manifest),
        'roles': sorted({m['roleName'] for m in manifest}),
        'skins': manifest,
    }
    with (stage / 'skins.json').open('w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    return len(manifest), missing


def import_pack(src, dst):
    reject_link(src)
    reject_link(dst)
    index_file = pack_path(src, 'skins-index.json')
    if not index_file.is_file():
        raise ValueError('找不到 skins-index.json')
    with index_file.open(encoding='utf-8') as f:
        if os.fstat(f.fileno()).st_nlink != 1:
            raise ValueError('不支持硬链接清单')
        index = json.load(f)
    thumbs = dst / 'thumbs'
    reject_link(thumbs)
    thumbs.mkdir(parents=True, exist_ok=True)
    # Stage the entire pack before publishing. Replace entries instead of opening
    # existing destination files, which might be links to unrelated data.
    with tempfile.TemporaryDirectory(dir=dst) as tmp:
        stage = Path(tmp)
        copied, missing = stage_pack(src, stage, index)
        for asset in stage.iterdir():
            if asset.name not in ('thumbs', 'skins.json'):
                asset.replace(dst / asset.name)
        for thumb in (stage / 'thumbs').iterdir():
            thumb.replace(thumbs / thumb.name)
        (stage / 'skins.json').replace(dst / 'skins.json')
    return copied, missing


def main():
    if len(sys.argv) < 3:
        print('用法: python import_skins.py <源目录 incoming> <目标目录 static/skins>')
        return 1
    dst = Path(sys.argv[2]).absolute()
    try:
        copied, missing = import_pack(Path(sys.argv[1]).absolute(), dst)
    except (OSError, ValueError) as exc:
        print(f'导入失败：{exc}', file=sys.stderr)
        return 1
    print('已复制 %d 张（缺失 %d），生成清单：%s' % (copied, missing, dst / 'skins.json'))
    return 0


if __name__ == '__main__':
    sys.exit(main())
