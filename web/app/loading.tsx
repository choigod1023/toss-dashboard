/** 라우트 전환·새로고침 시 즉시 표시되는 골격.
 *
 *  서버에서 데이터를 모으는 동안(왕복 여러 번) 브라우저가 흰 화면으로
 *  멈춰 있으면 죽은 것처럼 보인다. Next 는 이 파일이 있으면 골격을
 *  먼저 스트리밍하고, 페이지가 준비되면 교체한다.
 */
export default function Loading() {
  return (
    <div className="wrap">
      <div className="head">
        <h1>토스 트레이딩 대시보드</h1>
        <div className="stamp"><span className="spin" />불러오는 중…</div>
      </div>

      <div className="sk-hero">
        <div className="sk sk-kicker" />
        <div className="sk sk-title" />
        <div className="sk sk-title short" />
        <div className="sk-chips">
          <div className="sk sk-chip" /><div className="sk sk-chip" />
        </div>
        <div className="sk-split">
          <div><div className="sk sk-line" /><div className="sk sk-line" /><div className="sk sk-line short" /></div>
          <div><div className="sk sk-line" /><div className="sk sk-line" /><div className="sk sk-line short" /></div>
        </div>
      </div>

      <div className="grid tiles">
        {[0, 1, 2, 3].map((i) => (
          <div className="card" key={i}>
            <div className="sk sk-line short" style={{ height: 12 }} />
            <div className="sk sk-value" />
            <div className="sk sk-line" style={{ height: 11, width: "70%" }} />
          </div>
        ))}
      </div>

      <div className="card" style={{ marginBottom: 14 }}>
        <div className="sk sk-line short" style={{ height: 12, marginBottom: 16 }} />
        {[0, 1, 2].map((i) => (
          <div className="sk sk-row" key={i} />
        ))}
      </div>

      <div className="grid two">
        {[0, 1].map((i) => (
          <div className="card" key={i}>
            <div className="sk sk-line short" style={{ height: 12, marginBottom: 14 }} />
            <div className="sk sk-chart" />
          </div>
        ))}
      </div>
    </div>
  );
}
