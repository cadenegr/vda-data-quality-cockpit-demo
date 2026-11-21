-- One-time setup for tables the pipeline must NOT overwrite.
-- Applied by: scripts/deploy.sh setup

-- Every decision a user takes in the cockpit (append-only audit log).
-- The latest row per (supplier_code, message_key) is the current state.
CREATE TABLE IF NOT EXISTS workspace.vda_dq.fix_log (
  ts             TIMESTAMP COMMENT 'when the decision was taken',
  user_email     STRING    COMMENT 'who decided (from the app login)',
  supplier_code  STRING,
  message_key    STRING    COMMENT 'delivery note key = findings.message_key',
  delivery_note  STRING,
  suggestion_id  STRING,
  date_window    STRING,
  field_path     STRING    COMMENT 'VDA field that is corrected',
  old_value      STRING,
  new_value      STRING    COMMENT 'transport order number chosen by the user',
  confidence     STRING    COMMENT 'high / medium as suggested by the detective',
  decision       STRING    COMMENT 'approved / rejected / undone',
  source         STRING    COMMENT 'zip:path of the original message'
) COMMENT 'Audit log of fixes approved or rejected in the DQ cockpit. Original messages are never modified.';

-- Carrier side of fix_log: delivery note numbers filled into truck statuses (VDA 4945).
-- The latest row per (carrier_code, message_key) is the current state.
CREATE TABLE IF NOT EXISTS workspace.vda_dq.fix_log_carrier (
  ts             TIMESTAMP COMMENT 'when the decision was taken',
  user_email     STRING    COMMENT 'who decided (from the app login)',
  carrier_code   STRING,
  message_key    STRING    COMMENT 'truck status key = findings.message_key (document_id#shipment_index)',
  suggestion_id  STRING,
  rule_id        STRING,
  field_path     STRING    COMMENT 'VDA field that is corrected',
  old_value      STRING,
  new_value      STRING    COMMENT 'delivery note number chosen by the user',
  confidence     STRING    COMMENT 'high / medium as suggested by the detective',
  decision       STRING    COMMENT 'approved / rejected / undone',
  source         STRING    COMMENT 'zip:path of the original message'
) COMMENT 'Audit log of carrier fixes approved or rejected in the DQ cockpit. Original messages are never modified.';

-- Corrections typed in by hand for problems without an automatic fix (append-only).
CREATE TABLE IF NOT EXISTS workspace.vda_dq.fix_log_manual (
  ts             TIMESTAMP COMMENT 'when the correction was noted',
  user_email     STRING    COMMENT 'who noted it (from the app login)',
  role           STRING    COMMENT 'supplier / carrier',
  company_code   STRING,
  rule_id        STRING,
  msg_type       STRING,
  message_key    STRING    COMMENT '= findings.message_key',
  ref_number     STRING    COMMENT 'human-readable reference shown in the cockpit',
  old_value      STRING,
  new_value      STRING    COMMENT 'correct value typed by the user',
  source         STRING    COMMENT 'zip:path of the original message'
) COMMENT 'Manual corrections noted in the DQ cockpit. Original messages are never modified.';

-- Login mapping: which app user sees which company. Empty = everybody browses the demo.
-- role: admin (team overview + every company) / supplier / carrier (pinned to `code`).
CREATE TABLE IF NOT EXISTS workspace.vda_dq.cockpit_users (
  user_email     STRING    COMMENT 'Databricks login email, lower case',
  role           STRING    COMMENT 'admin / supplier / carrier',
  code           STRING    COMMENT 'supplier_code or carrier_code (empty for admin)',
  name           STRING    COMMENT 'display name shown in the sidebar'
) COMMENT 'Maps DQ cockpit logins to a company. Maintained by hand by an admin.';
