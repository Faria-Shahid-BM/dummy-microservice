
    You are extracting structured information from a property valuation report.

    Extract the following fields and return ONLY valid JSON.

    Fields:
    valuation_company, description="The name of the company that conducted the valuation."
    valuation_in_name_of, description="The name of the entity for whom the valuation is conducted."
    property_address, description="The address of the property being evaluated."
    owned_by, description="The name of company that owns the property."
    valuation_date, description="The date on which the valuation was conducted.'"
    type_of_land, description="The classification of the land (e.g.,industrial, residential, commercial, agricultural,etc.)."
    status_of_land, description="The current status of the land (e.g. built, under construction, open)."
    valuation_type, description="The type of valuation being conducted.(eg.full scope, deskstop. hint: if desktop valuation is mentioned anywhere in the report, then it's a desktop valuation, else full scope)"
    assets_evaluated, description="The assets being evaluated in the entire valuation report (e.g., industrial property, commercial building,etc.)."
    land_value, description="The value of the land in terms of numbers."
    building_value, description="The value of the building in terms of numbers."
    construction_status, description="The status of the construction.(e.g., completed, in progress)"
    valuator_comments, description="All the comments made by the valuator regarding the property or valuation process. it will be found under the heading "Notes to the Report" or similar heading. if there are no comments, return null."



    Rules:
    - If a value is missing return null
    - Do not guess
    - All numeric fields MUST be numbers (no commas, no strings)
    - Dates MUST be in YYYY-MM-DD format
    - Return JSON only

    REPORT TEXT:
    {report_text}
    