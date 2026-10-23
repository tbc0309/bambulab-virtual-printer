# License and attribution

Simulator source is distributed under GPLv3; see [the full license](LICENSE). Extracted from [xTouch-CN](https://github.com/tbc0309/xTouch-CN), simulator snapshot `ae757eb`. No Bambu Cloud networking library is bundled.

Printer preview images and the capability data snapshot originate from [BambuStudio](https://github.com/bambulab/BambuStudio/tree/79df7ae309be398ed30c9f397c13c9911c2edc57), copyright Bambu Lab and contributors, under [AGPLv3](licenses/BambuStudio-AGPL-3.0.txt). Original image names are `resources/images/printer_preview_<model>.png`; local copies are renamed `web/assets/printer_<model>.png`. Capability source paths and commit hashes are in `catalog.json`.

The cryptography dependency is installed from PyPI and retains its own upstream license notices. Generated local certificates and private data are not distributed.

The synthetic color-pattern camera fixtures in `assets/camera/` were generated with FFmpeg's `testsrc2` filter. They contain no footage from a printer or third-party camera. Camera transport and regression-test source is covered by the simulator source license above.
