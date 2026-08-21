# Petrovaradin Info Archive

Odvojen istorijski kolektor za pronalaženje i arhiviranje vesti o Petrovaradinu.
RSS/feed obrada pripada drugom projektu i nije deo ovog kolektora.

## Šta radi

- otkriva URL-ove iz sitemapova;
- koristi Selenium za interne pretrage portala i `next` paginaciju;
- uvozi URL-ove iz ručno izvezenog CSV/TXT fajla;
- normalizuje i deduplikuje URL-ove u SQLite bazi;
- poštuje `robots.txt`, ograničava brzinu i ponavlja privremene greške;
- čuva originalni HTML kao `.html.gz` i prateći JSON sa hashom i HTTP metapodacima.

## Instalacija (PowerShell)

```powershell
cd "D:\PycharmProjects\petrovaradin_info_archive"
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
petrovaradin-archive init
```

Selenium Manager iz Selenium 4 automatski pronalazi ili preuzima odgovarajući driver.
Chrome ili Firefox moraju biti instalirani. Ako automatsko preuzimanje nije dostupno,
driver treba postaviti u `PATH`.

## Prvi bezbedan probni prolaz za 021.rs

```powershell
petrovaradin-archive discover --provider selenium --site 021
petrovaradin-archive stats
```

Svi konfigurisani portali, redom:

```powershell
petrovaradin-archive discover --provider selenium
```

Prva faza koristi početne URL-ove internih pretraga i Selenium. Za svaku stranu čuvaju
se URL, naslov/tekst rezultata, URL search strane i redni broj strane. SQLite odmah
upisuje svaki rezultat, pa ponovno pokretanje ne pravi duplikate.

Selektori i URL interne pretrage nalaze se u `config/sites.yaml`. Portali menjaju HTML,
pa selektore treba proveriti i prilagoditi po sajtu. Generički `a[href]` hvata širi skup,
a baza uklanja duplikate.

## Ručni uvoz URL-ova

CSV treba da ima kolonu `url` ili `link`; TXT treba da ima jedan URL po redu:

```powershell
petrovaradin-archive import .\urls.csv --source manual_import
```

## Dodavanje portala

Dodati stavku u `config/sites.yaml` sa stabilnim ID-em, domenima, sitemapovima i,
po potrebi, Selenium pravilima:

```yaml
- id: primer
  name: Primer
  domains: [primer.rs, www.primer.rs]
  enabled: true
  sitemaps: [https://primer.rs/sitemap_index.xml]
  internal_search:
    enabled: true
    engine: selenium
    start_url: https://primer.rs/pretraga?q=Petrovaradin
    result_link_css: article h2 a
    next_css: a[rel='next']
    max_pages: 100
```

## Podaci

Runtime podaci su namerno izuzeti iz Git-a:

```text
data/archive.sqlite3
data/html/<domen>/<sha256>.html.gz
data/html/<domen>/<sha256>.json
```

Verifikacija i arhiviranje pending URL-ova:

```powershell
petrovaradin-archive download --limit 20
```

Status postaje `archived` samo kada HTTP HTML ili Selenium-renderovani page source
sadrži aktivnu ključnu reč. HTML se tada nepromenljivo čuva kao `.html.gz`. Status
`no_keyword` znači da je stranica dostupna, ali reč nije potvrđena; `retry` je privremena
greška, a `unavailable` trajna greška poput HTTP 404/410. Sačuvani `archive_path` ostaje
lokalna kopija čak i ako originalni URL kasnije nestane.

SQLite tabela `discoveries` pamti svaki izvor koji je pronašao URL, čak i kada je
sam URL već postojao u tabeli `urls`.

## Prenete vesti i adapteri

Posle arhiviranja HTML-a adapter izdvaja naslov, datum, glavni tekst, canonical URL i,
kada je eksplicitno naveden uz oznaku poput `Izvor` ili `Preneto`, URL originalne vesti.
Podržani su WordPress, Drupal i generički adapter; WordPress i Drupal se mogu i
automatski prepoznati. Izvor može eksplicitno zadati `adapter: wordpress` u
`config/sites.yaml`.

Sve kopije ostaju sačuvane kao zasebni arhivski dokazi. Identičan ili veoma sličan
tekst povezuje se preko `duplicate_group_id`, a redovna pretraga prikazuje primarni
rezultat i broj dodatnih kopija umesto ponavljanja iste vesti. Stare arhive se
analiziraju jednom, a nove automatski tokom preuzimanja:

```powershell
petrovaradin-archive analyze --limit 3000
petrovaradin-archive analyze --site razglas_news --limit 500
petrovaradin-archive duplicates --limit 100
```

BUKA je podešena kao web-search i WordPress izvor (`https://6yka.com/?s=petrovaradin`).
Za izvore koji
često prenose tuđe vesti postoji informativna oznaka `syndication_prone: true`; ona
ne utiče na dostupnost i ne briše sadržaj.

## Upravljanje izvorima

Izvori se sinhronizuju iz `config/sites.yaml` u SQLite tabelu `sources`. Operativne
promene se zatim rade u bazi i ostaju sačuvane:

```powershell
petrovaradin-archive sources list
petrovaradin-archive sources disable informer
petrovaradin-archive sources enable informer
petrovaradin-archive sources review-on informer
petrovaradin-archive sources public-off informer
petrovaradin-archive sources public-on informer
petrovaradin-archive sources block-add informer https://informer.rs/nezeljena-sekcija/
petrovaradin-archive sources block-remove informer https://informer.rs/nezeljena-sekcija/
```

Izvor sa `MANUAL_REVIEW` oznakom dobija status `awaiting_review` nakon uspešne provere
i čuvanja page source-a. Arhiva se pregleda komandom `archive-source ID`, pa odobrava
ili odbija:

```powershell
petrovaradin-archive review 123 approve
petrovaradin-archive review 123 reject
```

`ON HIDDEN MANUAL_REVIEW` znači: kolektor i dalje čuva dokaz, ali redovna pretraga ga
ne prikazuje. Posle pojedinačnih odobrenja izvor se može uključiti za javni prikaz sa
`sources public-on ID`; i tada se prikazuju samo odobreni `archived` redovi.

```powershell
petrovaradin-archive search Petrovaradin --limit 100
```

## Dokumenti, slike i potpuna sitemap provera

Arhiva čuva HTML page source, kao i povezane PDF/DOC/DOCX/ODT dokumente i slike.
Za PDF, DOCX, ODT i tekstualne formate izdvaja se tekst koji ulazi u redovnu pretragu.
Podrazumevani limiti su 30 MB po dokumentu, 8 MB po slici, 25 priloga i 60 MB svih
priloga po jednoj stranici. Menjaju se u `config/settings.yaml`.

```powershell
petrovaradin-archive assets --status archived --limit 100
petrovaradin-archive download-assets --kind document --limit 500
petrovaradin-archive download-assets --kind image --limit 500
petrovaradin-archive assets-requeue --status failed --kind document
petrovaradin-archive clean-urls
petrovaradin-archive robots-recheck
petrovaradin-archive audit-sources
petrovaradin-archive coverage
```

Za sajtove sa lošom internom pretragom postoji spora, resumable provera sadržaja
svakog URL-a iz sitemapova. Već provereni URL-ovi pamte se u tabeli `scan_checks`:

```powershell
petrovaradin-archive discover --provider scan --site skupstina_ns
petrovaradin-archive discover --provider scan --site nsurbanizam
```

Besplatna Google `site:` pretraga preko Seleniuma postoji bez API ključa, ali Google
može prikazati consent ili anti-bot stranicu i tada vratiti nula rezultata:

```powershell
petrovaradin-archive discover --provider google --site skupstina_ns
```

Postoji i DuckDuckGo HTML fallback bez API-ja. Pretraživač može privremeno vratiti
HTTP 403 ako se proverava mnogo izvora u jednom prolazu, pa ga pokretati po izvoru:

```powershell
petrovaradin-archive discover --provider ddg --site skupstina_ns
```

## Važne napomene

- Wayback i plaćeni API servisi nisu deo projekta.
- Aktivni search upiti su `Petrovaradin` i `Петроварадин`; rezultati sa njihovim
  gramatičkim izvedenicama (npr. `Petrovaradinski`) takođe se zadržavaju.
- Oficirac, Mišeluk, Tranžament, Tekije, Alibegovac i Vezirac evidentirani su kao
  kandidati za kasnije, ali još nisu uključeni u pretragu.
- `robots.txt` je uključen po defaultu. Ne isključivati ga bez pravne i etičke procene.
