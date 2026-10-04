# fix/source-006

- Republika je poslednja u `config/sites.yaml`. `run_last: true` je drži na kraju
  izabranih izvora i ako se redosled konfiguracije naknadno promeni.
- Selektor Republike izdvaja samo naslove rezultata pretrage, bez kategorija,
  preporuka i bočnih vesti koje su ulazile kroz opšti `article a[href]`.
- Selenium poredi skupove kanonskih URL-ova: redosled, ponovljeni linkovi,
  fragmenti i poznati parametri za praćenje ne utiču na poređenje.
- Ako nova stranica ponovi prethodnu ili neku raniju stranicu istog upita,
  završava se obrada tog sajta. Ne otvara se naredna stranica niti druga varijanta
  upita. Poruka sadrži `status=repeated_page`, broj obrađene stranice, njen URL,
  broj jedinstvenih linkova i upozorenje da je stranica verovatno nepostojeća.
- Delimično preklapanje ne prekida pretragu kada postoje drugi rezultati.
  Zaštita radi i uz `--full-scan`; ponavljanje ne potvrđuje potpunu pokrivenost
  pretrage u bazi. Nema fiksnog ograničenja na 10 stranica.

Provera: 31 test Selenium/source-005/source-006 prolazi, kao i test selektora
Republike na sačuvanom HTML-u. Živa provera počevši od stranice 10 pronašla je
18 rezultata, prešla na stranicu 11 i prekinula zbog istih 18 URL-ova.
Produkcijska baza tokom provere nije menjana.
