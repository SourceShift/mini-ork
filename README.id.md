
<h1 align="center">mini-ork</h1>

<p align="center">
  <strong>Sistem operasi tugas untuk agen AI — yang mewajibkan mereka membuktikan hasil kerjanya.</strong>
</p>

<p align="center">
  <a href="LICENSE"><img alt="License: Apache-2.0" src="https://img.shields.io/badge/license-Apache--2.0-blue.svg"></a>
  <a href="https://github.com/SourceShift/mini-ork/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/SourceShift/mini-ork/actions/workflows/ci.yml/badge.svg"></a>
  <a href="https://github.com/SourceShift/mini-ork/actions/workflows/codeql.yml"><img alt="CodeQL" src="https://github.com/SourceShift/mini-ork/actions/workflows/codeql.yml/badge.svg"></a>
  <img alt="Python 3.11 | 3.12" src="https://img.shields.io/badge/python-3.11%20%7C%203.12-blue.svg">
</p>

<p align="center"><a href="README.md">English</a> | <a href="README.zh-CN.md">简体中文</a> | <a href="README.ja.md">日本語</a> | <a href="README.ko.md">한국어</a> | <a href="README.es.md">Español</a> | <a href="README.pt-BR.md">Português (Brasil)</a> | <a href="README.de.md">Deutsch</a> | <a href="README.fr.md">Français</a> | <a href="README.ru.md">Русский</a> | <a href="README.hi.md">हिन्दी</a> | <a href="README.it.md">Italiano</a> | <a href="README.tr.md">Türkçe</a> | Bahasa Indonesia</p>

<p align="center">
  <a href="https://miniork.dev/media/mini-ork-demo.mp4">
    <img src="assets/mini-ork-demo.gif" alt="Run code-fix di IDE mini-ork: klasifikasi, perencanaan, implementasi, verifikasi, tiga lensa review, publikasi, lalu Keep atau Reject untuk setiap file yang berubah" width="900">
  </a>
</p>
<p align="center"><sub>Run <code>code-fix</code> di IDE mini-ork: klasifikasi → rencana → implementasi → verifikasi → tiga lensa review pada tiga keluarga model → publikasi, lalu setiap file yang berubah menunggu Keep atau Reject. Contoh run dari <a href="https://miniork.dev/">panduan interaktif</a> · <a href="https://miniork.dev/media/mini-ork-demo.mp4">video kualitas penuh</a>.</sub></p>

mini-ork mengubah sebuah tujuan menjadi run yang direncanakan, dieksekusi, dan
**diverifikasi** di atas armada model: **klasifikasi → rencana → eksekusi → verifikasi →
refleksi → peningkatan**. Setiap putusan berasal dari apa yang benar-benar dilakukan kode
saat dijalankan — tes, pemeriksaan tipe, skema, eksekusi di sandbox terisolasi — bukan dari
pendapat model tentang keluarannya sendiri.

Halaman ini adalah panduan instalasi. Untuk alasan di balik proyek ini, lihat
[Mengapa mini-ork ada](docs/WHY.md).

## Mulai cepat

macOS, Linux, atau WSL:

~~~bash
curl -fsSL https://raw.githubusercontent.com/SourceShift/mini-ork/main/install.sh | sh
~~~

Installer akan:

- meng-clone mini-ork ke `~/.local/share/mini-ork` (bisa diganti lewat `MINI_ORK_INSTALL_DIR`),
- membuat `.venv` lokal di dalam checkout dan memasang profil Python lengkap
  (CLI, sidecar web lokal, Crucible),
- menaruh `mini-ork` di PATH Anda melalui `~/.local/bin`,
- menawarkan wizard penggunaan pertama jika dijalankan di terminal.

Instalasi yang sama dari checkout yang sudah ada:

~~~bash
git clone --recurse-submodules https://github.com/SourceShift/mini-ork.git
cd mini-ork
make install
mini-ork version        # open a new terminal if PATH changed
~~~

`--recurse-submodules` mengisi `ui/`, yaitu crate IDE
([`SourceShift/mini-ork-ui`](https://github.com/SourceShift/mini-ork-ui)). Runtime Python
tidak membutuhkannya; lihat [docs/UI.md](docs/UI.md) untuk membangun IDE.

## Penggunaan pertama: `mini-ork onboard`

Satu perintah membawa mesin yang masih baru hingga memiliki workspace yang siap menjalankan
recipe. Perintah ini tidak pernah memanggil model dan tidak pernah mengeluarkan biaya —
setiap langkahnya adalah pemeriksaan lokal atau build lokal.

~~~bash
cd ~/my-project
mini-ork onboard
~~~

| Langkah | Yang terjadi |
|---|---|
| prasyarat | Memeriksa Python ≥ 3.11 dan git; mencatat `sqlite3` / `jq` jika tersedia |
| workspace | Membuat `.mini-ork/` di direktori saat ini (menjalankan `git init` bila perlu) |
| lane provider | Melaporkan apakah kredensial sudah tersimpan, atau menyebutkan perintah untuk menambahkannya |
| contextnest | *Opsional:* meng-clone, mem-build, dan menyiapkan config awal untuk layanan memori lokal |
| zed | *Opsional:* menyambungkan agent + server MCP mini-ork ke `settings.json` milik Zed |

Komponen opsional hanya ditawarkan jika toolchain-nya tersedia (`cargo`, `zed`), dan
masing-masing bisa dipilih secara eksplisit:

~~~bash
mini-ork onboard --with contextnest,zed      # force both
mini-ork onboard --no-contextnest --no-zed   # core only
mini-ork onboard --dry-run                   # print the plan, change nothing
mini-ork onboard -y                          # accept defaults (scripts, CI)
mini-ork onboard --json                      # machine-readable step results
mini-ork onboard --help                      # every flag
~~~

Kode keluar: `0` selesai (komponen yang dilewati tidak masalah), `1` komponen yang dipilih
gagal dalam mode `--strict`, `2` kesalahan penggunaan. Run non-interaktif tidak menampilkan
prompt: `-y` menerima semua nilai default.

### ContextNest — substrat memori lokal

Layanan HTTP berbasis Rust yang mengingat keputusan, kegagalan, hasil verifikasi, dan rilis
dari sesi-sesi sebelumnya. Wizard menyiapkan `config/contextnest.yaml` pada tier
**sederhana**; dengan `--secure`, wizard tidak menulis apa pun dan mewajibkan `CN_TOKEN`
sebelum layanan mau berjalan.

~~~bash
mini-ork onboard --with contextnest     # clone + cargo build --release + seed config
mini-ork cn status                      # tier, endpoint, reachability, latency
~~~

Secara manual: `git clone https://github.com/SourceShift/ContextNest.git`, lalu
`make cn-serve` (bind ke `127.0.0.1:28080`). Membutuhkan Rust 1.80+. Detail:
[docs/CONTEXTNEST-INTEGRATION.md](docs/CONTEXTNEST-INTEGRATION.md).

### Zed — integrasi editor

Menyambungkan `mini-ork acp` (agent eksternal) dan `mini-ork mcp-context` (konteks run,
pembelajaran, dan biaya yang bersifat read-only) ke Zed. Setiap penulisan ke
`settings.json` selalu didahului backup bertanda waktu.

~~~bash
mini-ork onboard --with zed     # or at any time:
mini-ork zed setup
mini-ork zed status
mini-ork zed uninstall
~~~

Lihat [docs/ZED.md](docs/ZED.md) dan [docs/UI.md](docs/UI.md).

## Workflow terverifikasi pertama Anda

~~~bash
cd ~/my-project
mini-ork onboard                      # initialize the project
mini-ork providers configure <lane>   # hidden prompt; keys never touch argv
mini-ork doctor                       # dependencies, environment, provider preflight

cp ~/.local/share/mini-ork/examples/01-hello-world/kickoff.md ./kickoff.md

# Check the wiring first: no model calls, no spend.
MINI_ORK_DRY_RUN=1 mini-ork run code-fix ./kickoff.md
mini-ork validate

# Then run it for real.
mini-ork run code-fix ./kickoff.md
~~~

Kickoff menyatakan repositori target, file yang boleh diubah, artefak yang diharapkan, serta
perintah atau aturan yang membuktikan keberhasilan. Artefak run disimpan di
`.mini-ork/runs/`, sedangkan state yang tercatat ada di `.mini-ork/state.db`. Tinjau
`.mini-ork/config/agents.yaml` untuk mengatur lane mana yang melayani peran mana.

### Buktikan perbaikan dengan satu perintah

`mini-ork certify` menerima sebuah perubahan beserta bug yang diklaim diperbaikinya,
mereproduksi bug tersebut pada kode Anda, membuat input yang tidak diantisipasi oleh
perubahan itu, menjalankan semuanya di sandbox Docker, lalu mengembalikan putusan lengkap
dengan buktinya:

~~~text
$ mini-ork certify --base base --head fix-cheat \
    --issue "median([1, 2, 3, 4]) returns 3, should return 2.5"
REFUTED  the fix does not generalise — only 0% of 4 invariants hold,
         which is the signature of a patch that special-cases the reported input
exit code: 1
~~~

Kode keluar `0` PROVEN, `1` REFUTED, `2` UNVERIFIED — perintah ini memilih abstain daripada
menebak, sehingga CI bisa menjadikannya gate. Membutuhkan Docker dan satu lane model.
Coba `bash examples/certify-demo/demo.sh`.

## Verifikasi, pembelajaran, dan peningkatan diri

<p align="center">
  <a href="https://miniork.dev/media/mini-ork-loops.mp4">
    <img src="assets/mini-ork-loops.gif" alt="Peta zona di miniork.dev: loop run, loop pembelajaran, loop perbaikan, dan loop RSI menyala bergantian, masing-masing menunjukkan apa yang mengalir antara engine, verifikasi, pembelajaran, dan pemulihan" width="900">
  </a>
</p>
<p align="center"><sub>Empat loop di sekeliling satu engine, dari <a href="https://miniork.dev/">panduan interaktif</a> (data contoh) · <a href="https://miniork.dev/media/mini-ork-loops.mp4">video kualitas penuh</a>.</sub></p>

GIF di bagian atas halaman ini adalah loop run. Tiga lapisan lain mengelilinginya.

### Verifikasi yang tidak bisa dikelabui agen dengan kata-kata

Setiap putusan adalah bukti eksekusi. Verifier sebuah recipe menjalankan pengaman
anti-tamper, test suite, replay terhadap base (tes harus gagal pada kode lama), dan
pemeriksaan kecukupan mutasi. Lane model kedua menulis tes spesifikasi secara buta dari
kickoff sebelum implementer mulai bekerja, dan implementer tidak bisa mengubahnya. Rantai
gate milik publisher menolak apa pun yang tidak lolos; model reviewer boleh memveto hasil,
tetapi tidak pernah bisa menciptakan status lolos. `certify` menerapkan sikap yang sama pada
satu perubahan dan menjawab PROVEN, REFUTED, atau UNVERIFIED bila tidak bisa membuktikan ke
arah mana pun. Pada korpus SWE-bench adversarial, 21 dari 21 putusan PROVEN terbukti benar
dengan 0 penyelesaian palsu; metode dan batasannya ada di [docs/RESULTS.md](docs/RESULTS.md),
mekanismenya di
[docs/architecture/verification-stack.md](docs/architecture/verification-stack.md).

### Pembelajaran yang hanya menyimpan apa yang terverifikasi

Setelah setiap run, trace diubah menjadi reward proses per node, dan reward positif wajib
disertai bukti dari verifier. Dari situ loop bercabang: advantage per lane yang menjadi bahan
belajar router (GRPO), gradien tekstual yang digali langkah refleksi menjadi pelajaran, dan
pemeriksaan konflik sebelum pelajaran apa pun ditulis. Hanya pelajaran terverifikasi yang
masuk ke prompt, setiap prompt membawa blok hasil belajar, dan ledger injeksi mencatat persis
apa yang diterima setiap node, sehingga kredit mengalir kembali ke pelajaran yang layak
menerimanya. Saat ini gradien dan tema terverifikasi memang sudah mengalir ke prompt,
sedangkan routing hasil belajar baru menentukan sebagian kecil rute. Siklus hidup dan status:
[docs/LEARNING-LOOP-LIFECYCLE.md](docs/LEARNING-LOOP-LIFECYCLE.md).

### Peningkatan diri yang dijaga gate

mini-ork mengedit mini-ork melalui graf recipe yang sama: `framework-edit` (self-edit
terverifikasi dengan tes spesifikasi buta), `self-improve` (pemindaian, lensa, sintesis,
implementasi, gate), `rsi-technique-review` (membaca literatur dan mengusulkan teknik untuk
dibangun), serta `goal-loop`. Perubahan hasil belajar harus lolos audit hackability dan audit
verifier, lalu gate non-regresi pada probe beku terhadap kelompok kontrol hasil rerun buta,
dan setiap versi yang dipromosikan menyimpan pointer rollback. Apply loop yang menulis ulang
prompt tanpa pengawasan dirilis dalam keadaan **nonaktif secara default** (lihat peringatan di
bawah). `framework-edit` adalah recipe yang paling sering dijalankan, dan `self-improve`
menemukan serta me-merge perbaikannya sendiri pada 2026-10-23. Detail:
[docs/RECURSIVE-SELF-IMPROVE.md](docs/RECURSIVE-SELF-IMPROVE.md),
[docs/SAFETY.md](docs/SAFETY.md), [docs/ECOSYSTEM.md](docs/ECOSYSTEM.md).

## Perintah sehari-hari

| Perintah | Fungsi |
|---|---|
| `mini-ork run <recipe> <kickoff.md>` | Menjalankan satu recipe dari awal hingga akhir |
| `mini-ork doctor` | Memeriksa dependensi, environment, dan preflight provider |
| `mini-ork validate` | Pemeriksaan statis sebelum run, dengan petunjuk `Fix:` |
| `mini-ork providers status <lane>` | Menampilkan keberadaan dan sumber kredensial (tidak pernah nilainya) |
| `mini-ork serve` | UI web dan API lokal di `127.0.0.1:7090` |
| `mini-ork garden` | Deteksi drift: tabrakan, orphan, run basi |
| `mini-ork recover` / `resume` | Menghidupkan kembali run yang gagal atau terputus |
| `mini-ork mcp` / `vault` | Registri server MCP; rantai backend secret vault |
| `mini-ork zed setup` / `cn status` | Penyambungan editor; tier dan keterjangkauan ContextNest |

Recipe awal: `code-fix`, `docs`, `refactor-audit`, `bug-audit-cmgk`,
`research-synthesis`, `spec-driven-delivery`. Lihat [recipes/](recipes/) dan
[panduan ekstensi](docs/EXTENSION.md).

## Memperbarui

Jalankan ulang installer, atau dari checkout jalankan `make install` — aman dijalankan
berulang kali: perintah ini memakai ulang `.venv`, memperbarui paket editable, memperbaiki
perintah terkelola, dan memverifikasi tool OS. Setelah itu `mini-ork update` menerapkan
migrasi database yang tertunda dan melaporkan config drift.

## Menghapus instalasi

~~~bash
mini-ork zed uninstall                     # remove the Zed entries
mini-ork automations scheduler uninstall   # only if you installed the OS tick
rm -rf ~/.local/share/mini-ork ~/.local/bin/mini-ork
~~~

State proyek tersimpan di `.mini-ork/` milik masing-masing proyek; hapus direktori itu di
proyek mana pun yang ingin Anda bersihkan. Checkout source dan riwayat git Anda tidak pernah
disentuh.

## Persyaratan

| | |
|---|---|
| OS | macOS, Linux, WSL. Windows native: pasang Python 3.11, Git, jq, yq, dan SQLite dengan `winget`, lalu jalankan `py -3 .\scripts\full_install.py` |
| Python | 3.11 atau 3.12 |
| Git | wajib |
| Docker | hanya untuk verifikasi dalam sandbox (`certify`, Crucible) |
| Rust 1.80+ | hanya untuk ContextNest dan crate IDE |
| Akses model | satu lane provider yang terkonfigurasi untuk run sungguhan; dry run tidak memerlukannya |

## Pelajari lebih lanjut

| | |
|---|---|
| [docs/WHY.md](docs/WHY.md) | Mengapa proyek ini ada; pengukuran; apa yang tidak diklaimnya |
| [docs/SAFETY.md](docs/SAFETY.md) | Sikap terhadap modifikasi diri dan gate yang menjaganya |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Desain sistem dan diagram komponen |
| [docs/operator](docs/operator) | Menjalankan mini-ork: variabel environment, troubleshooting |
| [docs/PYTHON-SDK.md](docs/PYTHON-SDK.md) | Menyematkan mini-ork ke aplikasi Anda sendiri |
| [docs/RESULTS.md](docs/RESULTS.md) | Metode dan ukuran sampel di balik angka-angka |
| [ROADMAP.md](ROADMAP.md) | Arah proyek ke depan |

> [!WARNING]
> mini-ork memiliki apply loop yang dapat menulis ulang prompt dan workflow-nya sendiri, lalu
> mempromosikan perubahan itu tanpa review manusia. Fitur ini **nonaktif secara default**
> (`MO_APPLY_ENABLED` dan `MO_AUTO_APPLY` harus sama-sama bernilai `1`), dan blast radius,
> batas, serta mekanisme rollback-nya didokumentasikan di [docs/WHY.md](docs/WHY.md#warning-this-system-modifies-itself-unattended).

## Kontribusi dan lisensi

mini-ork berlisensi **Apache-2.0**, © Amir Khakshour. Proyek ini masih tahap awal dan
berkelas riset: gunakan worktree khusus untuk perubahan framework, sertakan verifier untuk
setiap klaim perilaku, dan jalankan pemeriksaan terfokus untuk area yang Anda ubah. Lihat
[AGENTS.md](AGENTS.md) untuk alur kontribusi dan [GOVERNANCE.md](GOVERNANCE.md) untuk arah
proyek.
