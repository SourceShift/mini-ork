
<h1 align="center">mini-ork</h1>

<p align="center">
  <strong>Yapay zekâ ajanları için, yaptıkları işi kanıtlatan bir görev işletim sistemi.</strong>
</p>

<p align="center">
  <a href="LICENSE"><img alt="License: Apache-2.0" src="https://img.shields.io/badge/license-Apache--2.0-blue.svg"></a>
  <a href="https://github.com/SourceShift/mini-ork/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/SourceShift/mini-ork/actions/workflows/ci.yml/badge.svg"></a>
  <a href="https://github.com/SourceShift/mini-ork/actions/workflows/codeql.yml"><img alt="CodeQL" src="https://github.com/SourceShift/mini-ork/actions/workflows/codeql.yml/badge.svg"></a>
  <img alt="Python 3.11 | 3.12" src="https://img.shields.io/badge/python-3.11%20%7C%203.12-blue.svg">
</p>

<p align="center"><a href="README.md">English</a> | <a href="README.zh-CN.md">简体中文</a> | <a href="README.ja.md">日本語</a> | <a href="README.ko.md">한국어</a> | <a href="README.es.md">Español</a> | <a href="README.pt-BR.md">Português (Brasil)</a> | <a href="README.de.md">Deutsch</a> | <a href="README.fr.md">Français</a> | <a href="README.ru.md">Русский</a> | <a href="README.hi.md">हिन्दी</a> | <a href="README.it.md">Italiano</a> | Türkçe | <a href="README.id.md">Bahasa Indonesia</a></p>

<p align="center">
  <a href="https://miniork.dev/media/mini-ork-demo.mp4">
    <img src="assets/mini-ork-demo.gif" alt="mini-ork IDE'sinde bir code-fix çalıştırması: sınıflandırma, planlama, uygulama, doğrulama, üç inceleme merceği, yayımlama, ardından değişen her dosya için Keep ya da Reject" width="900">
  </a>
</p>
<p align="center"><sub>mini-ork IDE'sinde bir <code>code-fix</code> çalıştırması: sınıflandır → planla → uygula → doğrula → üç model ailesinde üç inceleme merceği → yayımla; ardından değişen her dosya Keep ya da Reject kararını bekler. <a href="https://miniork.dev/">Adım adım tanıtımdan</a> örnek bir çalıştırma · <a href="https://miniork.dev/media/mini-ork-demo.mp4">tam kaliteli video</a>.</sub></p>

mini-ork bir hedefi, bir model filosu üzerinde planlanan, yürütülen ve **doğrulanan** bir
çalıştırmaya dönüştürür: **sınıflandır → planla → yürüt → doğrula → değerlendir → iyileştir**.
Her hüküm, kodun çalıştığında gerçekte ne yaptığına dayanır — testler, tip denetimleri,
şemalar, yalıtılmış bir sandbox'ta yürütme — bir modelin kendi çıktısı hakkındaki görüşüne değil.

Bu sayfa kurulum kılavuzudur. Projenin arkasındaki gerekçe için
[mini-ork neden var?](docs/WHY.md) belgesine bakın.

## Hızlı başlangıç

macOS, Linux veya WSL:

~~~bash
curl -fsSL https://raw.githubusercontent.com/SourceShift/mini-ork/main/install.sh | sh
~~~

Kurulum betiği:

- mini-ork'u `~/.local/share/mini-ork` dizinine klonlar (`MINI_ORK_INSTALL_DIR` ile değiştirilebilir),
- checkout'a özel bir `.venv` oluşturur ve tam Python profilini kurar
  (CLI, yerel web sidecar'ı, Crucible),
- `mini-ork` komutunu `~/.local/bin` altından PATH'inize ekler,
- bir terminalde çalışıyorsa ilk çalıştırma sihirbazını önerir.

Mevcut bir checkout'tan aynı kurulum:

~~~bash
git clone --recurse-submodules https://github.com/SourceShift/mini-ork.git
cd mini-ork
make install
mini-ork version        # open a new terminal if PATH changed
~~~

`--recurse-submodules`, IDE crate'i olan `ui/` dizinini doldurur
([`SourceShift/mini-ork-ui`](https://github.com/SourceShift/mini-ork-ui)). Python
çalışma zamanının buna ihtiyacı yoktur; IDE'yi derlemek için [docs/UI.md](docs/UI.md) belgesine bakın.

## İlk çalıştırma: `mini-ork onboard`

Tek bir komut, sıfır bir makineyi tarif çalıştırabileceğiniz bir çalışma alanına taşır.
Hiçbir modeli çağırmaz, hiç para harcamaz — her adım yerel bir denetim ya da yerel bir derlemedir.

~~~bash
cd ~/my-project
mini-ork onboard
~~~

| Adım | Ne olur |
|---|---|
| ön koşullar | Python ≥ 3.11 ve git'i denetler; varsa `sqlite3` / `jq` araçlarını not eder |
| çalışma alanı | Geçerli dizinde `.mini-ork/` oluşturur (gerekirse `git init` çalıştırır) |
| sağlayıcı hatları | Kimlik bilgilerinin saklanıp saklanmadığını bildirir ya da onları ekleyen komutu söyler |
| contextnest | *İsteğe bağlı:* yerel bellek servisini klonlar, derler ve başlangıç yapılandırmasını oluşturur |
| zed | *İsteğe bağlı:* mini-ork'un ajanını + MCP sunucularını Zed'in `settings.json` dosyasına bağlar |

İsteğe bağlı bileşenler yalnızca araç zincirleri kuruluysa (`cargo`, `zed`) önerilir
ve her biri açıkça seçilebilir:

~~~bash
mini-ork onboard --with contextnest,zed      # force both
mini-ork onboard --no-contextnest --no-zed   # core only
mini-ork onboard --dry-run                   # print the plan, change nothing
mini-ork onboard -y                          # accept defaults (scripts, CI)
mini-ork onboard --json                      # machine-readable step results
mini-ork onboard --help                      # every flag
~~~

Çıkış kodları: `0` tamamlandı (atlanan bileşenler sorun değildir), `1` `--strict` altında
seçili bir bileşen başarısız oldu, `2` kullanım hatası. Etkileşimsiz bir çalıştırmada soru
sorulmaz: `-y` tüm varsayılanları kabul eder.

### ContextNest — yerel bellek altyapısı

Geçmiş oturumların neye karar verdiğini, neyde başarısız olduğunu, neyi doğruladığını ve
neyi teslim ettiğini hatırlayan bir Rust HTTP servisi. Sihirbaz **basit** katmanda
`config/contextnest.yaml` dosyasını oluşturur; `--secure` ile hiçbir şey yazmaz ve servis
başlamadan önce `CN_TOKEN` ister.

~~~bash
mini-ork onboard --with contextnest     # clone + cargo build --release + seed config
mini-ork cn status                      # tier, endpoint, reachability, latency
~~~

Elle kurmak için: `git clone https://github.com/SourceShift/ContextNest.git`, ardından
`make cn-serve` (`127.0.0.1:28080` adresini dinler). Rust 1.80+ gerektirir. Ayrıntılar:
[docs/CONTEXTNEST-INTEGRATION.md](docs/CONTEXTNEST-INTEGRATION.md).

### Zed — editör entegrasyonu

`mini-ork acp` (harici ajan) ve `mini-ork mcp-context` (salt okunur çalıştırma, öğrenme ve
maliyet bağlamı) bileşenlerini Zed'e bağlar. `settings.json` dosyasına yapılan her yazmadan
önce zaman damgalı bir yedek alınır.

~~~bash
mini-ork onboard --with zed     # or at any time:
mini-ork zed setup
mini-ork zed status
mini-ork zed uninstall
~~~

Bkz. [docs/ZED.md](docs/ZED.md) ve [docs/UI.md](docs/UI.md).

## İlk doğrulanmış iş akışınız

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

Bir kickoff dosyası hedef depoyu, izin verilen dosyaları, amaçlanan çıktıyı ve başarıyı
kanıtlayan komutu ya da kuralı belirtir. Çalıştırma çıktıları `.mini-ork/runs/` altına,
kaydedilen durum `.mini-ork/state.db` içine yazılır. Hangi hattın hangi role hizmet
edeceğini değiştirmek için `.mini-ork/config/agents.yaml` dosyasını inceleyin.

### Bir düzeltmeyi tek komutla kanıtlayın

`mini-ork certify` bir değişikliği ve onun düzelttiğini iddia ettiği hatayı alır, hatayı
kodunuz üzerinde yeniden üretir, değişikliğin hesaba katmadığı girdiler üretir, her şeyi
bir Docker sandbox'ında çalıştırır ve kanıtları ekli bir hüküm döndürür:

~~~text
$ mini-ork certify --base base --head fix-cheat \
    --issue "median([1, 2, 3, 4]) returns 3, should return 2.5"
REFUTED  the fix does not generalise — only 0% of 4 invariants hold,
         which is the signature of a patch that special-cases the reported input
exit code: 1
~~~

Çıkış kodları: `0` PROVEN, `1` REFUTED, `2` UNVERIFIED — tahmin yürütmek yerine hüküm
vermekten kaçınır, bu yüzden CI onu bir kapı olarak kullanabilir. Docker ve bir model hattı
gerektirir. Deneyin: `bash examples/certify-demo/demo.sh`.

## Doğrulama, öğrenme ve kendini iyileştirme

<p align="center">
  <a href="https://miniork.dev/media/mini-ork-loops.mp4">
    <img src="assets/mini-ork-loops.gif" alt="miniork.dev üzerindeki bölge haritası: çalıştırma döngüsü, öğrenme döngüsü, onarım döngüsü ve RSI döngüsü sırayla yanar; her biri motor, doğrulama, öğrenme ve kurtarma arasında neyin aktığını gösterir" width="900">
  </a>
</p>
<p align="center"><sub>Tek bir motorun etrafında dört döngü, <a href="https://miniork.dev/">adım adım tanıtımdan</a> (örnek veri) · <a href="https://miniork.dev/media/mini-ork-loops.mp4">tam kaliteli video</a>.</sub></p>

Sayfanın başındaki GIF çalıştırma döngüsüdür. Onun etrafında üç katman daha yer alır.

### Ajanların lafla geçiştiremeyeceği doğrulama

Her hüküm bir yürütme kanıtıdır. Bir tarifin doğrulayıcıları bir kurcalama koruması, test
paketi, taban kodda yeniden çalıştırma (test eski kodda başarısız olmalıdır) ve bir mutasyon
yeterliliği denetimi çalıştırır. İkinci bir model hattı, uygulayıcı başlamadan önce
kickoff'tan kör spec testleri yazar ve uygulayıcı bunları düzenleyemez. Yayımlayıcının kapı
zinciri geçemeyen hiçbir şeyi kabul etmez; inceleyici bir model bir sonucu veto edebilir ama
asla kendiliğinden bir geçiş üretemez. `certify` aynı tutumu tek bir değişikliğe uygular ve
PROVEN ya da REFUTED yanıtını verir; iki yönde de kanıtlayamadığında UNVERIFIED der. Düşmanca
hazırlanmış bir SWE-bench derleminde 21 PROVEN hükmünün 21'i de doğruydu ve 0 yanlış tamamlama
vardı; yöntem ve sınırları [docs/RESULTS.md](docs/RESULTS.md) belgesinde, mekanizmalar
[docs/architecture/verification-stack.md](docs/architecture/verification-stack.md) belgesinde.

### Yalnızca doğrulananı tutan öğrenme

Her çalıştırmadan sonra izler düğüm başına bir süreç ödülüne dönüşür ve pozitif bir ödül
doğrulayıcı kanıtıyla eşleşmek zorundadır. Döngü buradan dallanır: yönlendiricinin öğrendiği
hat başına avantajlar (GRPO), değerlendirme adımının derslere dönüştürdüğü metinsel gradyanlar
ve herhangi bir ders yazılmadan önce bir çelişki denetimi. Bir prompt'a yalnızca doğrulanmış
dersler ulaşır, her prompt öğrenilmiş bloğu taşır ve bir enjeksiyon defteri her düğümün tam
olarak ne aldığını kaydeder; böylece kredi, onu hak eden derse geri akar. Bugün gradyanlar ve
doğrulanmış temalar gerçekten prompt'lara ulaşıyor; öğrenilmiş yönlendirme ise rotaların hâlâ
küçük bir azınlığına karar veriyor. Yaşam döngüsü ve durum:
[docs/LEARNING-LOOP-LIFECYCLE.md](docs/LEARNING-LOOP-LIFECYCLE.md).

### Kapılarla denetlenen kendini iyileştirme

mini-ork, mini-ork'u aynı tarif grafı üzerinden düzenler: `framework-edit` (kör spec
testleriyle doğrulanan öz düzenlemeler), `self-improve` (tarama, mercekler, sentez, uygulama,
kapılar), `rsi-technique-review` (literatürü okur ve geliştirilecek teknikler önerir) ve
`goal-loop`. Öğrenilmiş bir değişiklik önce bir kandırılabilirlik denetiminden ve bir
doğrulayıcı denetiminden, ardından dondurulmuş sondalar üzerinde, kör yeniden çalıştırma
kontrol koluna karşı bir gerileme kapısından geçmelidir; terfi eden her sürüm bir geri alma
işaretçisi tutar. Prompt'ları gözetimsiz yeniden yazan uygulama döngüsü **varsayılan olarak
kapalı** gelir (aşağıdaki uyarıya bakın). `framework-edit` en çok çalıştırılan tariftir;
`self-improve` ise 2026-10-23'te kendi düzeltmesini bulup birleştirdi. Ayrıntılar:
[docs/RECURSIVE-SELF-IMPROVE.md](docs/RECURSIVE-SELF-IMPROVE.md),
[docs/SAFETY.md](docs/SAFETY.md), [docs/ECOSYSTEM.md](docs/ECOSYSTEM.md).

## Günlük komutlar

| Komut | Ne yapar |
|---|---|
| `mini-ork run <recipe> <kickoff.md>` | Tek bir tarifi baştan sona çalıştırır |
| `mini-ork doctor` | Bağımlılıkları, ortamı ve sağlayıcı ön denetimini kontrol eder |
| `mini-ork validate` | `Fix:` ipuçlarıyla statik, çalıştırma öncesi denetimler |
| `mini-ork providers status <lane>` | Kimlik bilgisinin varlığını ve kaynağını gösterir (değerlerini asla) |
| `mini-ork serve` | `127.0.0.1:7090` üzerinde yerel web arayüzü ve API |
| `mini-ork garden` | Sapma tespiti: çakışmalar, sahipsiz kayıtlar, bayat çalıştırmalar |
| `mini-ork recover` / `resume` | Başarısız olan ya da yarıda kalan bir çalıştırmayı diriltir |
| `mini-ork mcp` / `vault` | MCP sunucu kaydı; gizli bilgi kasası arka uç zinciri |
| `mini-ork zed setup` / `cn status` | Editör bağlantısı; ContextNest katmanı ve erişilebilirliği |

Başlangıç tarifleri: `code-fix`, `docs`, `refactor-audit`, `bug-audit-cmgk`,
`research-synthesis`, `spec-driven-delivery`. Bkz. [recipes/](recipes/) ve
[genişletme kılavuzu](docs/EXTENSION.md).

## Yükseltme

Kurulum betiğini yeniden çalıştırın ya da bir checkout içinden `make install` komutunu
çalıştırın — tekrar çalıştırmak güvenlidir: mevcut `.venv` ortamını yeniden kullanır,
düzenlenebilir paketi günceller, yönetilen komutu onarır ve işletim sistemi araçlarını
doğrular. Ardından `mini-ork update` bekleyen veritabanı migrasyonlarını uygular ve
yapılandırma sapmasını raporlar.

## Kaldırma

~~~bash
mini-ork zed uninstall                     # remove the Zed entries
mini-ork automations scheduler uninstall   # only if you installed the OS tick
rm -rf ~/.local/share/mini-ork ~/.local/bin/mini-ork
~~~

Proje durumu her projenin kendi `.mini-ork/` dizininde tutulur; kaldırmak istediğiniz yerde
onu silin. Kaynak checkout'larınıza ve git geçmişinize asla dokunulmaz.

## Gereksinimler

| | |
|---|---|
| İşletim sistemi | macOS, Linux, WSL. Yerel Windows: Python 3.11, Git, jq, yq ve SQLite'ı `winget` ile kurun, ardından `py -3 .\scripts\full_install.py` komutunu çalıştırın |
| Python | 3.11 veya 3.12 |
| Git | gerekli |
| Docker | yalnızca sandbox içinde doğrulama için (`certify`, Crucible) |
| Rust 1.80+ | yalnızca ContextNest ve IDE crate'i için |
| Model erişimi | gerçek çalıştırmalar için yapılandırılmış bir sağlayıcı hattı; deneme çalıştırmaları hiçbirini gerektirmez |

## Daha fazlası

| | |
|---|---|
| [docs/WHY.md](docs/WHY.md) | Projenin neden var olduğu; ölçümler; neyi iddia etmediği |
| [docs/SAFETY.md](docs/SAFETY.md) | Kendi kendini değiştirme konusundaki tutum ve onu sınırlayan kapılar |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Sistem tasarımı ve bileşen diyagramları |
| [docs/operator](docs/operator) | mini-ork'u çalıştırmak: ortam değişkenleri, sorun giderme |
| [docs/PYTHON-SDK.md](docs/PYTHON-SDK.md) | mini-ork'u kendi uygulamanıza gömün |
| [docs/RESULTS.md](docs/RESULTS.md) | Rakamların arkasındaki yöntemler ve örneklem büyüklükleri |
| [ROADMAP.md](ROADMAP.md) | Projenin nereye gittiği |

> [!WARNING]
> mini-ork, kendi prompt'larını ve iş akışlarını yeniden yazıp değişikliği insan incelemesi
> olmadan terfi ettirebilen bir uygulama döngüsü içerir. Bu döngü **varsayılan olarak kapalıdır**
> (hem `MO_APPLY_ENABLED` hem de `MO_AUTO_APPLY` değerinin `1` olması gerekir); etki yarıçapı,
> üst sınırları ve geri alma yolları [docs/WHY.md](docs/WHY.md#warning-this-system-modifies-itself-unattended) belgesinde anlatılmıştır.

## Katkı ve lisans

mini-ork **Apache-2.0** lisanslıdır, © Amir Khakshour. Proje erken aşamada ve araştırma
düzeyindedir: framework değişiklikleri için ayrı bir worktree kullanın, her davranış
iddiasının yanında bir doğrulayıcı bulundurun ve değiştirdiğiniz yüzeye yönelik odaklı
denetimleri çalıştırın. Katkı iş akışı için [AGENTS.md](AGENTS.md), proje yönü için
[GOVERNANCE.md](GOVERNANCE.md) belgesine bakın.
