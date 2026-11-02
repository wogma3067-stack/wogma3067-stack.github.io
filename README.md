# 태풍 지구본 (Typhoon Atlas)

실시간 태풍, 바람, 구름, 낮과 밤을 3D 지구본에 그리는 정적 웹페이지입니다.

- 주소: https://wogma3067-stack.github.io/
- 바람: NOAA GFS 0.5° 지상 10 m 바람. GPU에서 바람 입자 약 16만 개(모바일 약 6만 5천 개)를 흘립니다.
- 태풍: 일본기상청(북서태평양), GDACS(JTWC·NHC 등 전 세계)
- 구름: [live-cloud-maps](https://github.com/matteason/live-cloud-maps) 실시간 구름 지도 (Contains modified EUMETSAT data), 3시간 간격
- 낮과 밤: 브라우저의 현재 시각으로 태양 위치를 계산
- 지구 영상: NASA Blue Marble, Black Marble(도시 불빛)

## 자료 갱신

`.github/workflows/update-data.yml`이 매시간 `scripts/update_data.py`를 실행해 `data/`의
`wind.png`, `wind.json`, `storms.json`을 갱신합니다. 자료가 바뀌었을 때만 커밋하고,
직전 커밋도 자동 갱신 커밋이면 하나로 합쳐 저장소가 커지지 않게 합니다.
구름 영상은 페이지가 직접 불러옵니다.

로컬에서 자료를 새로 받으려면:

```sh
pip install eccodes numpy pillow
python scripts/update_data.py
python -m http.server
```

예보와 관측 자료는 각 기관의 발표를 옮긴 것으로, 공식 정보는 기상청 등 원 발표 기관을 확인하세요.
