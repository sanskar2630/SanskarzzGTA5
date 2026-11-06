"""Build a static, playable Vercel deployment from the verified public release."""
import argparse
import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import tempfile

from download_game import digest, extract_payload, target_path

HERE = Path(__file__).resolve().parent
PREFIX = 'mirror/playgta5.com/'


def patch_worker(code):
    old = 'if (wanted.size) await batchFetch(full, true, wanted);'
    new = 'for (const r of full) { if (wanted.has(r.id)) await ensureBlocks(r.id, r.a, r.b, true); }'
    if code.count(old) != 1:
        raise ValueError('Unrecognized I/O worker: expected exactly one batch prefetch call')
    return code.replace(old, new)


def patch_page(code):
    old = 'const q = new URLSearchParams(location.search);'
    new = old + "\n// Conservative hosted defaults; explicit URL settings still take precedence.\nfor (const [key, value] of Object.entries({low:'1', fps:'30', res:'1280x720'})) { if (!q.has(key)) q.set(key, value); }"
    if code.count(old) != 1:
        raise ValueError('Unrecognized game page: expected exactly one query parser')
    return code.replace(old, new)



def patch_homepage(code):
    """Apply Sanskar Studios branding to the generated game homepage."""
    import re

    if len(re.findall(r"</head\s*>", code, flags=re.IGNORECASE)) > 1:
        raise ValueError("Unrecognized game page: multiple closing head tags")

    title = "GTA V Browser Experience | Sanskar Studios"
    description = (
        "Explore a community-hosted GTA V browser experience, with gameplay modes "
        "and setup information. Sanskar Studios is an independent website brand and "
        "is not affiliated with Rockstar Games or Take-Two."
    )

    # Replace an existing title, or add one to the document head.
    title_pattern = r"<title\b[^>]*>.*?</title\s*>"
    if re.search(title_pattern, code, flags=re.IGNORECASE | re.DOTALL):
        code = re.sub(title_pattern, f"<title>{title}</title>", code, count=1,
                      flags=re.IGNORECASE | re.DOTALL)
    else:
        head_match = re.search(r"<head\b[^>]*>", code, flags=re.IGNORECASE)
        if not head_match:
            raise ValueError("Unrecognized game page: missing head element")
        code = code[:head_match.end()] + f"\n<title>{title}</title>" + code[head_match.end():]

    # Remove prior conflicting SEO / social / icon declarations to avoid duplicates.
    remove_patterns = [
        r'<meta\b(?=[^>]*\bname\s*=\s*["\'](?:description|application-name|robots|theme-color|twitter:card|twitter:title|twitter:description)["\'])[^>]*>',
        r'<meta\b(?=[^>]*\bproperty\s*=\s*["\']og:[^"\']+["\'])[^>]*>',
        r'<link\b(?=[^>]*\brel\s*=\s*["\'](?:canonical|icon|shortcut icon|apple-touch-icon)["\'])[^>]*>',
        r'<script\b(?=[^>]*\btype\s*=\s*["\']application/ld\+json["\'])[^>]*>.*?</script\s*>',
    ]
    for pattern in remove_patterns:
        code = re.sub(pattern, "", code, flags=re.IGNORECASE | re.DOTALL)

    metadata = f"""
<meta name="description" content="{description}">
<meta name="application-name" content="Sanskar Studios">
<meta name="robots" content="index, follow">
<meta name="theme-color" content="#171421">
<link rel="canonical" href="https://sanskar2630.online/">
<link rel="icon" type="image/png" sizes="64x64" href="/favicon.png">
<link rel="apple-touch-icon" href="/favicon.png">
<meta property="og:type" content="website">
<meta property="og:site_name" content="Sanskar Studios">
<meta property="og:title" content="{title}">
<meta property="og:description" content="{description}">
<meta property="og:url" content="https://sanskar2630.online/">
<meta property="og:image" content="https://sanskar2630.online/sanskar-studios-logo.png">
<meta name="twitter:image" content="https://sanskar2630.online/sanskar-studios-logo.png">
<meta name="twitter:card" content="summary">
<meta name="twitter:title" content="{title}">
<meta name="twitter:description" content="{description}">
<script type="application/ld+json">
{{
  "@context": "https://schema.org",
  "@type": "WebSite",
  "name": "Sanskar Studios",
  "alternateName": "Sanskar2630",
  "url": "https://sanskar2630.online/"
}}
</script>
"""
    if re.search(r"</head\s*>", code, flags=re.IGNORECASE):
        code = re.sub(r"</head\s*>", metadata + "\n</head>", code, count=1, flags=re.IGNORECASE)
    else:
        # Some static snapshots omit an explicit head close; add metadata after title.
        code = re.sub(r"(</title\s*>)", r"\\1" + metadata, code, count=1, flags=re.IGNORECASE)
    return code

def finish(output):
    page = output / 'index.html'
    page.write_text(patch_homepage(patch_page(page.read_text(encoding='utf-8'))), encoding='utf-8', newline='\n')
    # Show the Sanskar Studios logo on the game homepage without intercepting game controls.
    page_code = page.read_text(encoding='utf-8')
    brand_overlay = '''
<style id="sanskar-studios-brand-style">
#sanskar-studios-brand{position:fixed;z-index:20;top:12px;left:12px;width:min(150px,24vw);max-height:100px;object-fit:contain;opacity:.96;pointer-events:none;filter:drop-shadow(0 2px 8px rgba(0,0,0,.72))}
@media(max-width:600px){#sanskar-studios-brand{width:min(104px,22vw);top:8px;left:8px}}
</style>
<img id="sanskar-studios-brand" src="/sanskar-studios-logo.png" alt="Sanskar Studios" aria-hidden="true">
'''
    if 'id="sanskar-studios-brand"' not in page_code:
        if re.search(r'</body\s*>', page_code, flags=re.IGNORECASE):
            page_code = re.sub(r"</body\s*>", brand_overlay + "\n</body>", page_code, count=1, flags=re.IGNORECASE)
        else:
            page_code += brand_overlay
    page.write_text(page_code, encoding='utf-8', newline='\n')
    shutil.copyfile(HERE / 'public' / 'sanskar-studios-logo.png', output / 'sanskar-studios-logo.png')
    favicon = HERE / 'public' / 'favicon.png'
    if not favicon.is_file():
        raise ValueError('Missing branding asset: public/favicon.png')
    shutil.copyfile(favicon, output / 'favicon.png')
    workers = list((output / 'b').glob('*/io_worker.js'))
    if len(workers) != 1:
        raise ValueError('Expected one versioned I/O worker')
    worker = workers[0]
    worker.write_text(patch_worker(worker.read_text(encoding='utf-8')), encoding='utf-8', newline='\n')
    help_dir = output / 'help'
    help_dir.mkdir()
    shutil.copyfile(HERE / 'public/index.html', help_dir / 'index.html')
    print('Ready: playable game at /; download/help page at /help/', flush=True)


def build(output, source=None):
    inventory = json.loads((HERE / 'file-inventory.json').read_text(encoding='utf-8'))
    manifest = json.loads((HERE / 'download-manifest.json').read_text(encoding='utf-8'))
    records = [r for r in inventory['files'] if r['path'].startswith(PREFIX)]
    if output.exists():
        raise ValueError(f'Output already exists; choose a fresh directory: {output}')
    output.parent.mkdir(parents=True, exist_ok=True)
    if source:
        output.mkdir()
        for record in records:
            relative = record['path'][len(PREFIX):]
            original = target_path(source, relative)
            if not original.is_file() or original.stat().st_size != record['bytes'] or digest(original) != record['sha256']:
                raise ValueError(f'Source verification failed: {relative}')
            destination = target_path(output, relative)
            destination.parent.mkdir(parents=True, exist_ok=True)
            if relative == 'index.html' or relative.endswith('/io_worker.js'):
                shutil.copyfile(original, destination)  # Patched files must never share an inode with the original.
            else:
                try:
                    os.link(original, destination)
                except OSError:
                    shutil.copyfile(original, destination)
        print(f'Verified {len(records)} local game files.', flush=True)
    else:
        # One archive at a time keeps peak disk usage close to the 21 GB extracted game.
        with tempfile.TemporaryDirectory(prefix='game-build-', dir=output.parent) as temporary:
            stage = Path(temporary)
            for part in manifest['parts']:
                archive = target_path(stage, part['name'])
                selected = [r for r in records if r['part'] == part['name']]
                required = part['bytes'] + sum(r['bytes'] for r in selected) + 128 * 1024**2
                if shutil.disk_usage(stage).free < required:
                    raise ValueError(f'Insufficient build disk space for {part["name"]}')
                print(f'Downloading {part["name"]} ({part["bytes"]:,} bytes)', flush=True)
                subprocess.run(['curl', '--fail', '--location', '--retry', '4', '--silent', '--show-error', '--output', str(archive), part['url']], check=True)
                if archive.stat().st_size != part['bytes'] or digest(archive) != part['sha256']:
                    raise ValueError(f'Release checksum mismatch: {part["name"]}')
                extract_payload(archive, stage, selected, windows=False)
                archive.unlink()
                print(f'Verified and extracted {len(selected)} game files from {part["name"]}', flush=True)
            (stage / 'mirror/playgta5.com').rename(output)
    finish(output)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, help='verified existing playgta5.com directory (local testing)')
    parser.add_argument('--output', type=Path, default=HERE / 'web-output')
    args = parser.parse_args()
    build(args.output.resolve(), args.source.resolve() if args.source else None)
