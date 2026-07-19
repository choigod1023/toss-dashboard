import "./globals.css";
export const metadata = { title: "토스 트레이딩 대시보드" };
export default function RootLayout({ children }: { children: React.ReactNode }) {
  return <html lang="ko"><body>{children}</body></html>;
}
