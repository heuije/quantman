// plotly.js-dist-min 은 타입을 동봉하지 않아 최소 선언만 둔다(Plotly.react/purge 사용).
declare module "plotly.js-dist-min" {
  const Plotly: {
    react: (el: HTMLElement, data: unknown[], layout?: unknown, config?: unknown) => Promise<unknown>;
    purge: (el: HTMLElement) => void;
  };
  export default Plotly;
}
