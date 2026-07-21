return {
  name = "service-entitlement",
  fields = {
    { config = {
        type = "record",
        fields = {
          { required_service = { type = "string", required = true } },
        },
      },
    },
  },
}
