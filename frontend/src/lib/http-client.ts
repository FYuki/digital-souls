export const requestHttp = async (
  url: string,
  init: RequestInit | undefined,
  error: (response: Response) => Error | Promise<Error>,
  body: 'json' | 'json-or-null' | 'none',
): Promise<unknown> => {
  const response = await fetch(url, init)
  if (!response.ok) throw await error(response)
  if (body === 'none') return undefined
  if (body === 'json-or-null' && response.status === 204) return null
  return response.json()
}
