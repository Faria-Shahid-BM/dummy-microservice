local cjson = require "cjson.safe"

local ServiceEntitlementHandler = {
  -- must run AFTER the jwt plugin (priority 1005) so the signature is
  -- already verified by the time this reads the claims
  PRIORITY = 899,
  VERSION = "1.0.0",
}

function ServiceEntitlementHandler:access(conf)
  local auth_header = kong.request.get_header("authorization")
  if not auth_header or not auth_header:find("^Bearer ") then
    return kong.response.exit(401, { message = "Unauthorized" })
  end

  local token = auth_header:sub(8)
  local payload_b64 = token:match("^[^.]+%.([^.]+)%.")
  if not payload_b64 then
    return kong.response.exit(401, { message = "Unauthorized" })
  end

  payload_b64 = payload_b64:gsub("-", "+"):gsub("_", "/")
  local pad = #payload_b64 % 4
  if pad > 0 then
    payload_b64 = payload_b64 .. string.rep("=", 4 - pad)
  end

  local ok, decoded = pcall(ngx.decode_base64, payload_b64)
  if not ok or not decoded then
    return kong.response.exit(401, { message = "Unauthorized" })
  end

  local claims = cjson.decode(decoded)
  local allowed = false
  if claims and claims.services then
    for _, s in ipairs(claims.services) do
      if s == conf.required_service then
        allowed = true
      end
    end
  end

  if not allowed then
    return kong.response.exit(403, { message = "not entitled to " .. conf.required_service })
  end
end

return ServiceEntitlementHandler
