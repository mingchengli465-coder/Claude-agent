# fonts

`NotoSansSC-VF.ttf` — Noto Sans SC, the variable (wght 100–900) build from
[google/fonts](https://github.com/google/fonts/tree/main/ofl/notosanssc),
licensed under the SIL Open Font License 1.1.

It is committed here on purpose: the cover renderer must not depend on whatever
fonts a server happens to have installed, and a missing CJK font renders every
character as tofu. One variable file covers every weight the cover uses
(Medium, Bold, Black), so there is nothing else to add.
