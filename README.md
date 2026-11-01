# wifich

Находит самый свободный канал Wi-Fi 2.4 ГГц и переключает на него модем BDCOM одной командой.

[English below](#english)

## Зачем

В многоквартирном доме на 2.4 ГГц работают десятки соседских сетей. Wi-Fi начинает тормозить и обрываться, хотя интернет и модем в порядке: просто соседи заняли тот же канал. Чтобы это исправить, обычно надо смотреть эфир отдельным приложением, заходить в веб-интерфейс модема и менять канал вручную. А через пару недель соседи переезжают на другие каналы, и всё повторяется.

`wifich` делает это сам:

1. Сканирует эфир через встроенный в macOS `system_profiler`.
2. Считает помехи на каналах 1, 6 и 11 — только они не пересекаются друг с другом. Учитывает уровень сигнала соседей и ширину их каналов (20/40 МГц).
3. Если текущий канал заметно хуже лучшего (более чем в 2 раза), заходит в модем, меняет канал и проверяет, что Mac переподключился на новом.

Если менять нечего, модем он не трогает.

## Требования

- macOS. Анализ эфира работает только здесь; `--force` меняет канал и на других системах, но без проверки результата.
- Python 3, сторонние библиотеки не нужны.
- Mac подключён к Wi-Fi этого модема по 2.4 ГГц. Если он на 5 ГГц, текущий канал модема неизвестен.
- Пароль от веб-интерфейса модема.

## Какие модемы подходят

Проверено на одном модеме BDCOM с веб-интерфейсом на базе Realtek: страница `/admin/wlbasic.asp`, форма `formWlanSetup`. На других прошивках может не заработать. Проверить свой модем безопасно, он ничего не меняет:

```sh
wifich --dry-run
```

Если у вас заработало на другой модели или не заработало, напишите в [issues](https://github.com/pwnz22/wifich/issues). Вывод `--dry-run` содержит имя вашей сети, а файл из `--dump` может содержать другие настройки модема — уберите личное перед публикацией.

## Установка

```sh
git clone https://github.com/pwnz22/wifich.git ~/wifich
```

Добавьте в `~/.zshrc`:

```sh
alias wifich='python3 ~/wifich/bdcom-channel.py'
export BDCOM_ROUTER='http://192.168.1.1'   # адрес, по которому открывается веб-интерфейс модема
export BDCOM_PASS='ваш-пароль'
export BDCOM_KEEP_OPTIONS='2.4 GHz (B+G+N),AP,20MHz,Upper,100%,Disabled,FCC(1)'   # подставьте свои значения
```

Затем выполните `source ~/.zshrc`. Откуда взять значения для `BDCOM_KEEP_OPTIONS`, написано [ниже](#bdcom_keep_options).

| Переменная | Что это | Обязательна |
|---|---|---|
| `BDCOM_ROUTER` | адрес модема | да, кроме `--scan` |
| `BDCOM_PASS` | пароль от веб-интерфейса | да, кроме `--scan` |
| `BDCOM_USER` | логин | нет, по умолчанию `admin` |
| `BDCOM_KEEP_OPTIONS` | текущие значения списков настроек | да на проверенном модеме, кроме `--scan` |

## Использование

```sh
wifich              # анализ и переключение, если есть канал заметно лучше
wifich --scan       # только анализ эфира, модем не трогаем
wifich --dry-run    # анализ и вход в модем: показать, что будет отправлено, ничего не меняя
wifich --force 6    # поставить канал 6 без анализа
wifich --dump       # сохранить страницу настроек в bdcom-wlan.html для отладки
```

Пример вывода `wifich --scan`:

```text
Соседние сети 2.4 ГГц:
  канал  1 (40 МГц)  уровень неизвестен
  канал  6 (20 МГц)  уровень неизвестен
  канал  6 (20 МГц)  уровень неизвестен
  канал  8 (20 МГц)  уровень неизвестен
  канал 11 (40 МГц)  уровень неизвестен
  канал 11 (20 МГц)  -66 dBm  ← похоже, сам модем, не учитываю

Помехи по каналам (меньше — лучше):
   1:       3   ← лучший
   6:      13
  11:       4   ← сейчас
```

При смене канала модем перезапускает Wi-Fi, и связь пропадёт на несколько секунд.

## BDCOM_KEEP_OPTIONS

Модем при сохранении принимает всю форму настроек целиком, а не только канал. Прошивка выставляет значения выпадающих списков через JavaScript, поэтому скрипт не видит их в HTML. Без этой переменной он напишет «Не удалось определить текущие значения полей» и ничего не отправит, чтобы не сбить настройки.

Откройте в веб-интерфейсе модема страницу WLAN Basic Settings и перечислите через запятую значения всех выпадающих списков, кроме канала, так, как они там подписаны: диапазон, режим, ширина канала, боковая полоса, мощность, лимит клиентов, регион. Пример из установки взят с настроек конкретного модема, не копируйте его вслепую. Затем сверьте результат:

```sh
wifich --dry-run
```

Команда покажет, что будет отправлено, и ничего не изменит. Если потом поменяете настройки в веб-интерфейсе, обновите и эту переменную, иначе `wifich` вернёт старые значения.

## Ограничения

- Работает только с 2.4 ГГц, 5 ГГц не трогает.
- Выбирает канал по одному замеру в одной точке: эфир рядом с Mac и у модема может отличаться.
- macOS часто не показывает уровень сигнала соседних сетей. Тогда скрипт считает его равным -75 dBm, и оценка получается приблизительной.
- Вторую SSID самого модема определяет эвристически: тот же канал и похожий сигнал.
- Модем общается по HTTP без шифрования, поэтому запускайте скрипт только в своей сети. Пароль берётся из переменной окружения и никуда, кроме `BDCOM_ROUTER`, не отправляется.

## English

`wifich` finds the least congested 2.4 GHz Wi-Fi channel and switches a BDCOM modem to it with one command. It's for apartment buildings, where neighbours' networks pile onto the same channel and Wi-Fi slows down even though the connection itself is fine.

It scans the air with macOS `system_profiler`, scores channels 1, 6 and 11 by neighbours' signal and channel width, and logs in to the modem only if the current channel is more than twice as congested as the best one.

**Requirements:** macOS, Python 3 (no dependencies), and a Mac connected to the modem over 2.4 GHz.

**Compatibility:** tested on one BDCOM modem with a Realtek-based web UI (`/admin/wlbasic.asp`, `formWlanSetup`). Other firmware may not work. `wifich --dry-run` checks yours without changing anything.

```sh
git clone https://github.com/pwnz22/wifich.git ~/wifich

# add to ~/.zshrc
alias wifich='python3 ~/wifich/bdcom-channel.py'
export BDCOM_ROUTER='http://192.168.1.1'   # your modem's web UI address
export BDCOM_PASS='your-password'
export BDCOM_KEEP_OPTIONS='2.4 GHz (B+G+N),AP,20MHz,Upper,100%,Disabled,FCC(1)'   # use your own values
```

```sh
wifich              # analyse, and switch if a clearly better channel exists
wifich --scan       # analyse only, never touches the modem
wifich --dry-run    # log in and show what would be sent, change nothing
wifich --force 6    # set channel 6 without analysis
```

`BDCOM_KEEP_OPTIONS` matters: the modem takes the whole settings form on save, and the firmware sets its dropdowns through JavaScript, so the script can't read them from the HTML. Without this variable it refuses to submit rather than risk resetting your settings. List every dropdown except the channel, as labelled on the WLAN Basic Settings page and comma-separated, then check the result with `wifich --dry-run`. The example above comes from one specific modem, so don't copy it blindly. The program's messages are in Russian.

## License

[MIT](LICENSE)
