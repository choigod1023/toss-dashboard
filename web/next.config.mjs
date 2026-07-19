/** @type {import('next').NextConfig} */
export default {
  // 상위 디렉터리에 다른 lockfile 이 있어 워크스페이스 루트를 잘못 잡는다 → 고정
  turbopack: { root: import.meta.dirname },
};
