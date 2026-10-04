# Promene na fix/source-005

## Poznati URL-ovi i kraći obilazak

Selenium i WordPress API učitavaju poznate kanonske URL-ove iz baze pre početka
pretrage. Log razlikuje `existing` (ranije u bazi), `duplicate` (ponovljen u ovom
obilasku), `accepted` i razloge odbacivanja. Praćenje ne menja download status:
poznati `pending` i `retry` URL-ovi ostaju u redu za preuzimanje.

`scan` sada proverava bazu pre otvaranja članka iz sitemapa. Prethodno je proveravao
samo svoju evidenciju skeniranih dokumenata. Obične pretrage otvaraju stranice
rezultata; same po sebi ne otvaraju svaki pronađeni članak.

Za internu pretragu kojoj je **potvrđen redosled od najnovijeg ka najstarijem**:

```powershell
.\.venv\Scripts\petrovaradin-archive.exe discover --provider selenium --site ID_SAJTA --newest-first
```

Alternativno postaviti `internal_search.newest_first: true` u konfiguraciji izvora.
Ovo ne menja redosled koji sajt vraća: opciju uključiti tek kada je redosled proveren.
WordPress API izričito zahteva `orderby=date&order=desc` i automatski koristi ovaj režim.

Prvo pokretanje pravi punu osnovu. Tek posle zabeleženog završetka iste pretrage,
dve uzastopne stranice sa isključivo ranije poznatim prihvatljivim URL-ovima
zaustavljaju obilazak (`status=known_pages`). Nov URL ili stranica bez prihvatljivih
URL-ova prekida niz. Limit stranica, ponovljena stranica, timeout i zaštita ne beleže
uspešan završetak. Prethodna nepotpuna prikupljanja ne smatraju se punom osnovom.
Latinični i ćirilični upiti imaju odvojenu evidenciju; promene filtera i selektora
zahtevaju novu osnovu. Evidencija se čuva u tabeli `search_coverage`.

Google CSE i infinite-scroll ne koriste rano zaustavljanje na osnovu poznatih
stranica. Za proveru naknadno dodatih starih članaka ili promenjenog indeksa koristiti
`--full-scan`. Ova opcija isključuje skraćivanje obilaska, ali zadržava proveru duplikata
i zadati `--max-pages` limit. Rano zaustavljanje je optimizacija, nije garancija da sajt
nikada nije ubacio nov rezultat među stare članke.

## Buka i KoSSev: opcija bez Chrome-a

```powershell
.\.venv\Scripts\petrovaradin-archive.exe discover --provider wordpress --site buka_6yka --site kossev
```

Provider pretražuje javni WordPress REST API, oba podešena pisma, i prati
`X-WP-TotalPages`. Koristi postojeći HTTP klijent, ograničenje brzine i robots pravila.
Ne rešava CAPTCHA i ne zaobilazi blokade. HTTP 403/429 ili HTML umesto JSON-a jesu
greške, a ne prazni rezultati. Selenium sa ručnom proverom ostaje dostupan.

Provera 2026-10-04: Buka API je vratio 403. KoSSev API je na pojedinačan zahtev
vratio JSON (`X-WP-TotalPages: 49` uz `per_page=1`), ali robots.txt vraća 403,
zbog čega redovan provider trenutno staje pre API zahteva. Alternativa je ugrađena,
ali uspešno prikupljanje sa oba sajta trenutno nije potvrđeno. Postojeći `--provider ddg`
može otkriti deo indeksiranih URL-ova bez otvaranja tih sajtova; nije potpuna arhiva.

Parametri API-ja: https://developer.wordpress.org/rest-api/reference/posts/

## Srbija Sport i provere

Selektor je ograničen na Google CSE rezultate. Pre klika se kontrola paginacije
dovodi u vidljivo polje; ako drugi element presretne klik, aktivira se ista stvarna
kontrola preko JavaScript-a. Zatim se čeka promena skupa rezultata.

Živa provera tri stranice uspela je: **26 različitih URL-ova na stranicama 1, 2 i 3**.
Oba prelaska su reprodukovala presretnut klik i uspešno nastavila rezervnim klikom.
Proba nije upisivala u produkcionu bazu niti otvarala članke.

Početno stanje grane: 44 testa prolaze, 18 pada (postojeći source-002 HTTP i
selektorski testovi). Te neusaglašenosti drugih izvora nisu deo ove promene.
Posle izmene: **55 prolazi, 17 pada**; svi novi testovi prolaze, a postojeći test
selektora Srbija Sporta je popravljen. Preostalih 17 pada i na početnom stanju.
