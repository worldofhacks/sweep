export function hostHeaderIsAllowed(value, configuredOrigin) {
  if (typeof value !== 'string' || value.length === 0) return false
  const origin = new URL(configuredOrigin)
  const authorities = new Set([origin.host.toLowerCase()])
  if (!origin.port && origin.protocol === 'http:') {
    authorities.add(`${origin.hostname}:80`.toLowerCase())
  }
  if (!origin.port && origin.protocol === 'https:') {
    authorities.add(`${origin.hostname}:443`.toLowerCase())
  }
  return authorities.has(value.toLowerCase())
}
