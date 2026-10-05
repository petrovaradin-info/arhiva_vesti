# Paralelni download i prioritet izvora

Izvori sa uključenim `manual_review` sada su poslednji u listama izvora,
pokrivenosti, statistici, URL-ovima, prilozima, obradi teksta i discovery redu.
Download primenjuje prioritet pre `--limit`; trenutna zastavica u bazi ima prednost.
Izvori se ne preskaču: obrađuju se nakon običnih izvora iz izabranog reda.

```powershell
.\.venv\Scripts\python.exe -u -m petrovaradin_archive.cli download --limit 10000 --mode selenium --workers 3
```

`--limit` je ukupan broj URL-ova, ne limit po radniku. Podrazumevano je jedan
radnik. Svaki izvor se obrađuje serijski, a do tri različita izvora paralelno,
sa zasebnim browserima i konekcijama. Ako izabrani red sadrži samo jedan izvor,
koristiće samo jednog radnika. Izvori za ručnu proveru počinju tek po završetku
ostalih izabranih izvora. `--site` i dalje ograničava rad na jedan izvor.

Jedna `download` komanda po bazi: OS zaključavanje odbija drugo pokretanje;
paralelizacija se podešava preko `--workers`. Zaključavanje se oslobađa i pri
padu procesa. Stara, već pokrenuta verzija ne poznaje ovo zaključavanje, pa je
pre pokretanja nove treba zaustaviti. Već arhivirani URL-ovi se ne preuzimaju ponovo.
Ctrl+C otkazuje čekajuće poslove; aktivni radnici završavaju tekući URL i zatvaraju
sesije. Sa više radnika lokalni brojači napretka u logu odnose se na pojedinačni izvor.

Validacija: 17 ciljanih testova prolazi. Kompletan skup: 81 prolazi, 16 postojećih
grešaka HTTP pretrage/selektora (potvrđene i na neizmenjenom projektu).
