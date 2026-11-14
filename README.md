# GiftStream

**TikTok LIVE steruje Minecraft Parkour i Dropper.** GiftStream odbiera zdarzenia LIVE, dopasowuje je do gotowego presetu i wykonuje bezpieczne akcje na lokalnym serwerze Minecraft.

## Co zawiera GiftStream

- Dwa gotowe tryby lokalnej gry: **Minecraft Parkour 100 Levels** oraz **Minecraft Dropper** z generowaną areną pięciu szybów i wodnymi lądowiskami. Dropper działa na vanilla i nie pobiera map/pluginów z niezaufanych źródeł.
- Reguły parkour można edytować: push, awans/cofnięcie poziomu, reset i anty-reset oraz sześć nowych efektów widzów — sprint, spowolnienie, ciemność, wysoki skok z bezpiecznym opadaniem, krótka tarcza i oczyszczenie z negatywnych efektów. Wszystkie wybierzesz jako akcję w kreatorze; panel Minecraft pozwala też testować je lokalnie bez zdarzenia LIVE. Cennik domyślny: push 1–29 monet, zmiana poziomu 30–999, reset i anty-reset od 1000; progi opierają się wyłącznie na jawnej cenie TikToka.
- Dropper ma osobny preset z akcjami: push, wyższy/niższy szyb, powrót na start, sprint, spowolnienie, ciemność, wysoki skok, tarcza i oczyszczenie. Test akcji działa z panelu Minecraft bez TikTok LIVE. Wybór vanilla to celowa decyzja: działanie nie wymaga pluginów Bukkit/Paper, które wymagałyby zmiany serwera i niezależnego audytu.
- Jednoklikowe przygotowanie lokalnego serwera vanilla z wyborem najnowszej stabilnej lub jednej z ostatnich wersji Minecrafta. GiftStream pobiera i weryfikuje serwer z oficjalnego manifestu Mojang, izoluje świat dla każdej wersji oraz pokazuje adres `localhost:25565`, który można skopiować. Mapa działa jako datapack bez niezweryfikowanych pluginów; instalacja naprawia dwa niezgodne zasoby item modifier i dodaje HUD poziomu obok istniejącego timera. Oryginalna mapa była przygotowana dla 26.2; poprawka usuwa zgłoszony błąd ładowania zasobów w 26.3, ale nie oznacza pełnej certyfikacji mapy dla wszystkich wersji. GiftStream wykrywa Javę w launcherach albo instaluje per-user zgodną wersję automatycznie; wymagane są internet przy pierwszej konfiguracji i samodzielna akceptacja Minecraft EULA. Szczegóły: [`docs/MINECRAFT.md`](docs/MINECRAFT.md).
- Opcjonalne przekazywanie treści komentarzy LIVE do czatu Minecraft. Tekst kodowany jest jako `tellraw` — nie jest wykonywany jako polecenie.
- **Nieoficjalny** konektor TikTokLive. GiftStream nie generuje fikcyjnych zdarzeń ani liczników; uruchamia się rozłączony i czeka na ręczne połączenie kanału. Przy zmianie nazwy kanału najpierw zatrzymuje poprzednie połączenie i zeruje statystyki, a potem zaczyna zbierać zdarzenia nowego LIVE. Połączenie zależy od aktualnej dostępności biblioteki i TikToka; GiftStream ponawia zerwane połączenia z czytelnym statusem i rosnącym odstępem, rozpoznaje kanał offline i sprawdza go ponownie co 2–5 minut, a przy limicie usługi podpisującej wstrzymuje bezcelowe próby.
- Diagnostyka obejmuje połączenia, wejściowe zdarzenia, reguły i akcje Parkour, instalację Java/serwera/mapy, RCON, błędy API oraz błędy interfejsu; logi JVM serwera są dołączane do panelu Minecraft. Logi można kopiować z panelu głównego lub zakładki Minecraft; diagnostyka nie dopisuje treści komentarzy LIVE.
- Biblioteka może przechować do 1000 giftów z bieżącego katalogu TikToka; wyszukuj prezenty po nazwie zarówno w bibliotece, jak i bezpośrednio w kreatorze reguł. Grafiki są serwowane przez bezpieczny proxy do CDN TikToka z odświeżeniem katalogu po błędzie ładowania; wpis bez dostępnej grafiki nadal pozostaje widoczny. Monety widza i diamenty twórcy są rozdzielone, a niepodane ceny nie są zgadywane.
- Osobne studio dźwięków LIVE z wyszukiwarką MyInstants, odsłuchem i wyborem jednym kliknięciem. Można też wgrać własny plik MP3, WAV, OGG, M4A, AAC lub FLAC (do 10 MB), ustawić publiczny URL HTTPS, głośność i TTS. Wybrany dźwięk ze Studio można przypisać do konkretnej reguły. Nakładki OBS mają sześć układów oraz edytowalną paletę, kolor, skalę, tytuł i widoczne wskaźniki.
- Alerty dźwiękowe, synteza mowy, widżety OBS oraz szacunkowa wartość nagród w PLN. Szacunek nie jest oficjalną stawką ani obietnicą wypłaty TikToka.

## Aplikacja Windows i aktualizacje

**GiftStream dla Windows to jeden samodzielny plik EXE.** Pobierz [GiftStream.exe](https://github.com/rejson59/GiftStream/releases/latest/download/GiftStream.exe) i uruchom go — osobny instalator ani launcher nie są potrzebne. Aplikacja otwiera natywne okno z osadzonym WebView2 bez czekania na sprawdzenie sieci ani utworzenie skrótu; skrót GiftStream w menu Start przygotowuje w tle. Ponowne uruchomienie przy działającej aplikacji nie otwiera drugiego okna. Python nie musi być zainstalowany; WebView2 musi być dostępny w Windows.

Aplikacja sprawdza aktualizacje podczas uruchamiania oraz co 30 minut. Zweryfikowana nowa wersja jest pobierana i automatycznie uruchamiana; w trakcie restartu GiftStream może na chwilę przestać odbierać zdarzenia, a po restarcie może być potrzebne ponowne połączenie z LIVE. Dane są przechowywane w `%LOCALAPPDATA%\GiftStream`. Nowa instalacja jest czysta i nie importuje danych z poprzednich instalacji.

Nowe wydania buduje GitHub Actions z taga `vX.Y.Z`; artefakt to GiftStream.exe i suma kontrolna. Plik nie jest podpisany Authenticode, więc Windows SmartScreen może pokazać ostrzeżenie.

## Uruchomienie deweloperskie

Wymagany Python 3.10+.

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

Otwórz `http://localhost:8000`. GiftStream uruchamia się rozłączony; połącz prawdziwy kanał TikTok LIVE, aby rozpocząć naliczanie statystyk. Nakładki OBS są dostępne pod `/overlay?template=grid`, `list`, `minimal` lub `streamer`.

## Bezpieczeństwo i ograniczenia

- Lokalny parkour nasłuchuje na `127.0.0.1`; nie wystawiaj portu RCON do Internetu.
- GiftStream nie wykonuje treści czatu jako komendy serwera. Relaying komentarzy jest domyślnie wyłączony i działa tylko po ręcznym włączeniu.
- TikTokLive nie jest oficjalnym API i może się zepsuć; automatyczne ponawianie połączenia nie odtwarza zdarzeń, które wystąpiły podczas rozłączenia.
- Integracja MyInstants nie korzysta z oficjalnego API; wyszukiwanie nieoficjalnie odczytuje publiczną stronę i zależy od jej dostępności.
- Lokalna wersja nie wymaga Supabase ani Vercel. Szczegóły TikTok LIVE: [`docs/TIKTOK.md`](docs/TIKTOK.md).
