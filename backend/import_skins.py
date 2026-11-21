# -*- coding: utf-8 -*-
"""把皮肤包里的 webp / 缩略图导入项目，并生成 static/skins/skins.json 清单。

用法：
    python import_skins.py "C:\\path\\to\\art\\incoming" "C:\\path\\to\\project\\static\\skins"

只读取源目录，只写入目标目录，不改动源文件。
"""
import json
import os
import shutil
import sys


def main():
    if len(sys.argv) < 3:
        print('用法: python import_skins.py <源目录 incoming> <目标目录 static/skins>')
        return 1

    src = os.path.abspath(sys.argv[1])
    dst = os.path.abspath(sys.argv[2])
    index_file = os.path.join(src, 'skins-index.json')
    if not os.path.exists(index_file):
        print('找不到 skins-index.json:', index_file)
        return 1

    with open(index_file, 'r', encoding='utf-8') as f:
        index = json.load(f)

    os.makedirs(os.path.join(dst, 'thumbs'), exist_ok=True)

    manifest = []
    copied = 0
    missing = 0
    for role in index.get('roles', []):
        role_id = role.get('roleId')
        role_name = role.get('name') or role_id
        for skin in role.get('skins', []):
            web_rel = skin.get('webp')
            thumb_rel = skin.get('thumbnail')
            if not web_rel or not thumb_rel:
                continue
            web_src = os.path.join(src, web_rel)
            thumb_src = os.path.join(src, thumb_rel)
            if not (os.path.exists(web_src) and os.path.exists(thumb_src)):
                missing += 1
                continue

            web_name = os.path.basename(web_rel)
            thumb_name = os.path.basename(thumb_rel)
            shutil.copy2(web_src, os.path.join(dst, web_name))
            shutil.copy2(thumb_src, os.path.join(dst, 'thumbs', thumb_name))
            copied += 1

            manifest.append({
                'id': skin.get('skinId'),
                'name': skin.get('name') or '',
                'roleId': role_id,
                'roleName': role_name,
                'web': '/static/skins/' + web_name,
                'thumb': '/static/skins/thumbs/' + thumb_name,
                'mobilePosition': skin.get('mobileObjectPosition') or '50% 50%',
            })

    manifest.sort(key=lambda x: (x['roleId'], x['id']))
    out = {
        'count': len(manifest),
        'roles': sorted({m['roleName'] for m in manifest}),
        'skins': manifest,
    }
    with open(os.path.join(dst, 'skins.json'), 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=1)

    print('已复制 %d 张（缺失 %d），生成清单：%s' % (copied, missing, os.path.join(dst, 'skins.json')))
    return 0


if __name__ == '__main__':
    sys.exit(main())
