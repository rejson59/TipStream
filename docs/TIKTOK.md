# TikTok LIVE connection options

GiftStream offers the real TikTokLive connector and an optional external WebSocket adapter in **Creator Studio → Stream connection**. It starts disconnected and does not generate simulated activity.

Changing to a different TikTok username stops the previous connector, clears the current session counters/history, and only then starts receiving the new channel's data. The first valid channel connection also clears any old session/demo statistics. Manual disconnect remains disabled for automatic reconnect on the next app start.

## TikTokLive (free, unofficial)

Install dependencies, select **TikTok Live · free**, and enter the creator's `@username`. The integration uses the community-maintained `TikTokLive` Python package. It is not an official TikTok API and can be affected by TikTok protocol changes, connection limits or signing-service availability. No TikTok login/password is requested by GiftStream.

TikTok does not provide a public official API for reading arbitrary LIVE chat/gifts, so third-party clients may stop working or require changes. GiftStream keeps one client session per channel, can restore the last explicitly selected TikTok channel after an app restart, exposes connecting/reconnecting/offline state, cleans up the previous async websocket before retrying, retries ordinary dropped connections with increasing, interruptible delays, and checks an offline creator again after about two minutes, increasing to at most about five minutes between checks. Choosing **Rozłącz** turns off automatic reconnect on the next app start. Do not click Connect repeatedly while the status says it is retrying. The **Logi diagnostyczne** panels show failures across the whole app (including Minecraft setup, game actions, API and browser errors) as well as the connection and retry reason; use **Kopiuj logi** from the dashboard or Minecraft view to share them. Logs are also stored locally in the GiftStream data folder and redact common token/password fields.

TikTokLive v7 uses EulerStream's signing service. If logs show `SignatureRateLimitError` or `rate_limit_account_hour`, the signing service has hit its hourly connection-start limit; this is not a problem with the creator's username or LIVE status. GiftStream now pauses automatic retries for this specific error rather than repeatedly consuming quota. Add your own EulerStream signing key as `SIGN_API_KEY` in the local `.env` file (see [`.env.example`](../.env.example)), then restart GiftStream and connect again. Keep this key private; never paste it into shared logs or commit it. Without a key, wait for the service's quota to reset before manually retrying. Other transient disconnects still use backoff retries. Retries cannot recover events missed while disconnected or guarantee that every event is captured. Use only with streams you are authorized to monitor and in line with TikTok's current terms and policies.

## Local browser relay (experimental)

For a local-only alternative, download [`GiftStream-Browser-Relay.zip`](https://github.com/rejson59/GiftStream/releases/latest/download/GiftStream-Browser-Relay.zip), unpack and load its folder as an unpacked extension, then open the creator's public LIVE page in Chrome, and set the GiftStream address in the extension popup (desktop builds can choose a port other than 8000). The helper watches only newly rendered, visible LIVE chat/notice elements and posts normalized events to `127.0.0.1` at `/api/browser/event`; the endpoint rejects non-loopback clients. Game rules triggered by this source are off by default in the extension popup; verify event recognition before opting in. It does not read browser cookies, passwords, page storage, or private WebSocket frames and does not require a remote event vendor.

This is an experimental best-effort source: TikTok changes its page markup and localization, and some event types or messages that are not visible in the page can be missed. The extension cannot replay events from before it was installed or while the browser/page is closed. Check its popup for the last forwarded event and the local received count. Run one source at a time while game rules are enabled: if TikTokLive and Browser relay both report the same event, GiftStream may trigger the matching action twice because cross-source deduplication is not guaranteed.

## Managed WebSocket adapter

Select **Managed API** and configure `TIKTOOL_API_KEY` in `.env`. The adapter uses the provider's documented WebSocket connection format. A managed vendor may charge fees and has its own availability, terms and privacy policy. Do not commit API keys.

## Biblioteka oryginalnych giftów

GiftStream odświeża publiczny katalog TikToka i przechowuje do 1000 wpisów, także wtedy, gdy gift nie ma grafiki. Zapisuje ID, nazwę, diamenty twórcy, jawne pola ceny monet (jeżeli źródło je udostępnia), informację o serii i bezpośredni URL grafiki. Obrazki panelu i nakładki są pobierane przez lokalny proxy z ograniczeniem do zaufanych domen CDN TikToka, co pomaga przy blokowaniu hotlinków; po błędzie aplikacja raz próbuje odświeżyć katalog. Brak grafiki nie ukrywa prezentu. W bibliotece wpisz nazwę w pole wyszukiwania; w kreatorze reguł także wpisz nazwę w polu „Prezent”, aby filtrować listę zamiast przewijać. Cena monet nie jest zgadywana z diamentów — jeżeli TikTok jej nie dostarcza, nie da się jej rzetelnie wyliczyć automatycznie. Ceny i lista giftów zależą od regionu i zmian po stronie TikToka.

## Dźwięki i reguły

W menu **Dźwięki i alerty** wyszukaj publiczne dźwięki MyInstants, odsłuchaj je i wybierz. Tam można również wgrać własny plik audio MP3, WAV, OGG, M4A, AAC lub FLAC do 10 MB albo wkleić publiczny link HTTPS. W edytorze reguły wybierz „Użyj dźwięku wybranego w Studio”, aby przypisać aktualnie wybrany dźwięk do konkretnej akcji. Reguła zapisuje kopię wybranego adresu; po zmianie dźwięku w Studio ponownie zapisz regułę, jeśli chcesz zmienić także dźwięk akcji. Własne pliki są przechowywane lokalnie w danych GiftStream; starsze pliki pozostają, aby już zapisane reguły nie utraciły swojego dźwięku po zmianie globalnego alertu. Wyszukiwarka MyInstants jest nieoficjalna i zależy od ich strony oraz dostępności pliku.

## Orientacyjny kalkulator zarobku twórcy

Pulpit pokazuje szacunkową wartość pojedynczego giftu, sumę diamentów i estymowany zarobek z bieżącej sesji. Kwota używa **diamentów po stronie twórcy** z eventu (a jeśli ich brakuje — z zapisanego katalogu giftów), liczby powtórzeń zakończonej serii oraz edytowalnych stawek: `diamenty × USD/diament × kurs USD/PLN`. Domyślny punkt startowy to 0,005 USD za diament i kurs 4,00 PLN za USD; obie wartości można zmienić i są zapisywane lokalnie. Nie wyliczamy wypłaty z monet widza ani nie zakładamy, że monety i diamenty mają równą wartość.

To **szacunek**, nie oficjalny kurs ani kwota potwierdzona przez TikToka. [Polityka wirtualnych przedmiotów TikToka](https://support.tiktok.com/legal/virtual-items?lang=en) określa przeliczanie nagród według czynników ustalanych przez TikToka, bez gwarantowanej stałej stawki gotówkowej. Stawka początkowa w GiftStream jest jedynie edytowalnym założeniem; sprawdź własne saldo i podsumowanie LIVE. Szacunek nie odejmuje podatków, opłat wypłaty, prowizji ani innych korekt. Serie są księgowane dopiero przy zdarzeniu kończącym, żeby nie sumować kolejnych aktualizacji tej samej serii.

## Security notes

- Treat event payloads as untrusted input. GiftStream maps known event types to configured actions; it does not execute arbitrary TikTok comments as shell or game-server commands.
- Keep the API bound to a trusted/local network unless you configure a reverse proxy and authentication.
- When accepting remote events, set `GIFTSTREAM_BRIDGE_WEBHOOK_SECRET` and provide `X-GiftStream-Secret`.
- Avoid putting private TikTok account credentials in `.env` or the dashboard; this MVP only needs the public LIVE username for the unofficial connector.
