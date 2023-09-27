# Permissions migration logic and data structures
The Hive Metastore migration process will upgrade the following Assets:
- Tables ond DBFS root
- External Tables
- Views

We don't expect this process to be a "one and done" process. This typically is an iterative process and may require a few runs.

We suggest to keep track of the migration and provide the user a continuous feedback of the progress and status of the upgrade.

Here is the outline of the process:
1. The assessment runs and capture all the tables in HMS (Done).
   1. Each table is categorized based on the type and storage
   1. We use the "upgraded_to" table property to determine if the table was already upgraded to UC
1. The assessment generates a list of "recommended External Locations". These locations are used by the external tables and are required for an "in place" upgrade.
1. The assessment generates "databases" tables or an outline for a configuration file.
1. It can be used by the user to configure the database upgrade.

This is the structure we recommend:
| Column Name | Type | Description | Default Value |
| -------------- | ----- | ------------ |----|
| database | string | Original schema of the table | |
| upgrade_assessment | int | 0-Manual<br/>1-In Place<br/>2-CTAS<br/>3-Mixed| |
| target_catalog | string | Name of the Target Catalog | ucx_<workspace_id> |
| target_datbase | string | Name of the Target Database | original database name |
| workspace_id | string | A workspace ID of the HMS (Optional) | |
| views | int | 0-No Views <br/> 1-Views present | |
| upgrade_status | int | 0-Not Upgraded <br/> 1-Failed Upgrade <br/> 2-Partial Upgrade <br/> 3-Full Upgrade | 0 |
| upgrade_messages | string | Json with a list of all the upgrade errors. | empty |

1. We can use a notebook with IPYWidgets to update the table (or configuration file).
1. The user has to generate the required "External Locations"
1. The table upgrade step perform the following:
   1. Create the target Catalog/Databases required for the upgrade
   1. Performs Sync for all the "In Place" table upgrades
   1. Skips any table that is marked with the "upgraded_to" table property.
   1. Performs CTAS or Deep Clone for all the tables that cannot be upgraded in place (consider bringing history)
   1. Attempt at Migrating Views (upgrade 2 level references)
   1. Update the "upgraded_to" table property for the upgraded table.
1. The user review the upgrade results.
1. The user rerun the job as needed.