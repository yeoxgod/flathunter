/* Жёсткие фильтры по критериям. Неизвестные поля не отсекают объявление. */
var FH = (typeof FH !== "undefined") ? FH : {};

(function (FH) {
  "use strict";
  FH.totalPrice = (l) => (l.price === null || l.price === undefined) ? null : l.price + (l.adminRent || 0);

  FH.prefilter = function (l, s) {
    s = s || {};
    const tp = FH.totalPrice(l);
    if (tp !== null) {
      if (s.maxPrice && tp > s.maxPrice) return [false, `цена ${tp} > ${s.maxPrice}`];
      if (s.minPrice && tp < s.minPrice) return [false, `цена ${tp} < ${s.minPrice}`];
    }
    if (l.area !== null && l.area !== undefined) {
      if (s.minArea && l.area < s.minArea) return [false, `площадь ${l.area} < ${s.minArea}`];
      if (s.maxArea && l.area > s.maxArea) return [false, `площадь ${l.area} > ${s.maxArea}`];
    }
    if (s.rooms && s.rooms.length && l.rooms && !s.rooms.includes(l.rooms)) return [false, `комнат ${l.rooms}`];
    const districts = (s.districts || []).map((d) => String(d).toLowerCase());
    if (districts.length && l.location && !districts.some((d) => l.location.toLowerCase().includes(d))) {
      return [false, `район «${l.location}»`];
    }
    const text = (l.title + "\n" + l.description).toLowerCase();
    for (const kw of s.excludeKeywords || []) {
      if (kw && text.includes(String(kw).toLowerCase())) return [false, `стоп-слово «${kw}»`];
    }
    return [true, ""];
  };

  if (typeof module !== "undefined") module.exports = FH;
})(FH);
