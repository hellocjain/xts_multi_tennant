import * as Core from '/Users/chinmayajain/Desktop/acagarwalopenalgo/openalgo/frontend/node_modules/openalgo-charts/dist/openalgo-charts.mjs';
import * as Indicators from '/Users/chinmayajain/Desktop/acagarwalopenalgo/openalgo/frontend/node_modules/openalgo-charts/dist/openalgo-charts.indicators.mjs';
import * as Draw from '/Users/chinmayajain/Desktop/acagarwalopenalgo/openalgo/frontend/node_modules/openalgo-charts/dist/openalgo-charts.draw.mjs';
import * as Trade from '/Users/chinmayajain/Desktop/acagarwalopenalgo/openalgo/frontend/node_modules/openalgo-charts/dist/openalgo-charts.trade.mjs';
import * as Transform from '/Users/chinmayajain/Desktop/acagarwalopenalgo/openalgo/frontend/node_modules/openalgo-charts/dist/openalgo-charts.transform.mjs';

const OpenAlgoCharts = {
  ...Core,
  Indicators,
  Draw,
  Trade,
  Transform,
};

if (typeof window !== 'undefined') {
  window.OpenAlgoCharts = OpenAlgoCharts;
}
if (typeof globalThis !== 'undefined') {
  globalThis.OpenAlgoCharts = OpenAlgoCharts;
}

export default OpenAlgoCharts;
export { Core, Indicators, Draw, Trade, Transform };
