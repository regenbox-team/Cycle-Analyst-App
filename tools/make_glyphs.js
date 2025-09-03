#!/usr/bin/env node
// Generate MapLibre/Mapbox PBF glyph ranges from a TTF/OTF using @mapbox/fontnik
// Usage: node tools/make_glyphs.js path/to/Font-Regular.ttf "static/vendor/fonts/Inter Regular"

const fs = require('fs');
const path = require('path');
let fontnik;
try {
  // Prefer scoped package when available
  fontnik = require('@mapbox/fontnik');
} catch (e1) {
  try {
    // Fallback to unscoped package
    fontnik = require('fontnik');
  } catch (e2) {
    console.error('Cannot find fontnik. Install one of:');
    console.error('  npm i -D @mapbox/fontnik');
    console.error('  or');
    console.error('  npm i -D fontnik');
    process.exit(1);
  }
}

function usage() {
  console.error('Usage: node tools/make_glyphs.js <font-ttf> <out-dir> [maxRange]');
  console.error('Example: node tools/make_glyphs.js assets/Inter-Regular.ttf "static/vendor/fonts/Inter Regular" 4095');
}

async function main() {
  const [,, fontPath, outDir, maxRangeArg] = process.argv;
  if (!fontPath || !outDir) {
    usage();
    process.exit(1);
  }
  const maxRange = Number(maxRangeArg || 4095); // up to Basic Latin + Latin-1 + extended blocks
  if (!fs.existsSync(fontPath)) {
    console.error('Font not found:', fontPath);
    process.exit(1);
  }
  fs.mkdirSync(outDir, { recursive: true });
  const font = fs.readFileSync(fontPath);

  function range(start) {
    return new Promise((resolve, reject) => {
      const end = start + 255;
      fontnik.range({ font, start, end }, (err, data) => {
        if (err) return reject(err);
        const out = path.join(outDir, `${start}-${end}.pbf`);
        fs.writeFile(out, data, (werr) => werr ? reject(werr) : resolve());
      });
    });
  }

  for (let start = 0; start <= maxRange; start += 256) {
    process.stdout.write(`Writing ${start}-${start+255}.pbf\n`);
    // eslint-disable-next-line no-await-in-loop
    await range(start);
  }
  console.log('Done. Glyphs written to', outDir);
}

main().catch((e) => { console.error(e); process.exit(1); });
