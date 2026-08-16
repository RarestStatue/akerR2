-- All 32 codes observed across the 25 rent rolls (verified by full scan).
-- label_verified = false marks descriptions inferred from the code string; the
-- loader must not guess further. Unknown future codes are auto-inserted by the
-- loader as ('other', NULL, false) together with an `unknown_charge_code` warning.
INSERT INTO core.charge_code (charge_code, category, description, label_verified) VALUES
  ('RENT',    'rent',       'Base rent',                              true),
  ('RENTAFF', 'rent',       'Affordable / restricted base rent',      true),
  ('RENTRETL','rent',       'Retail / commercial base rent',          true),
  ('RENTHAP', 'subsidy',    'HAP (Section 8) rent portion',           true),
  ('SUBSIDY', 'subsidy',    'Rental subsidy',                         true),
  ('SEC8CRD', 'subsidy',    'Section 8 credit',                       true),
  ('MTM',     'fee',        'Month-to-month premium',                 true),
  ('PARKING', 'parking',    'Parking',                                true),
  ('CONPARK', 'concession', 'Parking concession',                     true),
  ('GARAGE',  'garage',     'Garage',                                 true),
  ('CONGAR',  'concession', 'Garage concession',                      true),
  ('STORAGE', 'storage',    'Storage',                                true),
  ('CONSTOR', 'concession', 'Storage concession',                     true),
  ('PETFEE',  'pet',        'Pet fee (one-time)',                     true),
  ('PETFEEM', 'pet',        'Pet fee (monthly)',                      true),
  ('CONPETM', 'concession', 'Monthly pet concession',                 true),
  ('AMENITY', 'amenity',    'Amenity fee',                            true),
  ('CONAMEN', 'concession', 'Amenity concession',                     true),
  ('CONRENT', 'concession', 'Rent concession',                        true),
  ('CONEMP',  'concession', 'Employee rent concession',               true),
  ('TRASH',   'utility',    'Trash / valet trash',                    true),
  ('WATER',   'utility',    'Water / sewer',                          true),
  ('UTILCOM', 'utility',    'Commercial utility reimbursement',       true),
  ('SALESTX', 'tax',        'Sales tax',                              true),
  ('RETXEST', 'tax',        'Real estate tax escrow estimate',        true),
  ('CAMEST',  'cam',        'CAM estimate',                           true),
  ('CAMINSR', 'insurance',  'CAM insurance recovery',                 true),
  ('HOMEPCKG','service',    'Home package bundle (inferred)',         false),
  ('SDFEE',   'fee',        'SD fee (inferred)',                      false),
  ('W/D',     'service',    'Washer/dryer rental (inferred)',         false),
  ('BIKE',    'service',    'Bike storage (inferred)',                false),
  ('RNTPROF', 'other',      'Rent profile adjustment (inferred)',     false)
ON CONFLICT (charge_code) DO NOTHING;
