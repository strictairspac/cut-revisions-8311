export function route() {
  const [path, search] = (location.hash.slice(1) || 'cases').split('?');
  const [key, id] = path.split('/');
  return {key, id: Number(id) || 0, query: new URLSearchParams(search || '')};
}
export function go(to) {
  location.hash = to;
}
