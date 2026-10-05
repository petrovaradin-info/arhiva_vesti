# fix/source-007

Operativni logovi pretrage, preuzimanja i HTTP zahteva sada imaju lokalni datum,
vreme sa milisekundama i UTC pomak, na primer:

```text
[2026-10-04 18:30:16.375+02:00] [republika] status=loaded URL=...; trajanje=1.235s
[2026-10-04 18:30:17.401+02:00] [republika] status=all_duplicates; strana=2; svi URL-ovi rezultata su duplikati; prelazim na sledeći sajt; URL=...
```

Trajanja se mere monotoničkim satom za pokretanje browsera, navigaciju, obradu
stranice, HTTP zahtev i izvršavanje sakupljača. Trajanje obrade stranice obuhvata
čekanje rezultata i upise pronađenih URL-ova; učitavanje URL-a ima zasebno trajanje.
HTTP trajanje uključuje podešenu pauzu između zahteva. Višelinijske greške imaju
vreme na svakoj liniji. Strukturirani CLI izveštaji, poput JSON izlaza, zadržavaju format.

Nova politika zamenjuje čekanje na dve poznate stranice: jedna neprazna stranica
sa isključivo ranije poznatim ili u ovom pokretanju viđenim rezultatima završava
sajt. URL-ovi izvan domena i odbačeni filterima nisu rezultati; stranica sa samo
takvim linkovima ne aktivira ovaj prekid. Ako postoji makar jedan nov prihvatljiv
URL, obilazak se nastavlja.

Pravilo važi za Selenium (uključujući Google CSE), Google site search, WordPress
API i HTTP pretragu. Preskaču se naredne stranice, druge varijante upita i preostali
sakupljači/dopunske pretrage istog sajta. Nastavlja se naredni izvor.
Važi i uz `--full-scan`, koji sada isključuje samo staro hronološko skraćivanje.
Ovakav prekid ne beleži da je kompletna istorija sajta pregledana i ne menja
download statuse poznatih URL-ova. Ovo je izričito tražena politika i može preskočiti
starije nepoznate rezultate iza stranice sa duplikatima.
