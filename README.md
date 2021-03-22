# External-Tox-Saver
A tool for externalizing your tox files. 


## Usage
Bring "Release/Externalizer.tox" into your project. It should immediately search for comps with the tag 'tox'. 
If this is the first time you've opened it, you can start externalizing your tox files.

### Externalizing Tox Files
Save your project (ctrl-s).

This will build a new directory in your project's folder to mirror your network. It will also create a "Backup" folder.
Anytime this saves a tox, it saves the primary file as well as incrementally saves the tox into the backup folder.

### Saving Tox Files
You can pulse either the "Save Changed Comps" button or the "Save All Comps" button. 

# System Tab

### Tox List
You can rebuild or clear the tox list anytime you want. To optimize this, I am not searching the whole project for external comps every time. When you externalize one, the path to the comp is placed into storage. When you save it loops through that list and saves what's there. This should work as long as your are externalizing via the tool itself.

### Autosave
There's a timer that triggers the AutoSaveNow method which incrementally saves all the "Dirty" comps. 

# Issues
There seems to be a bug with the comp class' dirty member. Once you save it it doesn't seem to actually make dirty False. Until that is fixed this will just keep saving everything that has changed within your entire session.

# Contributors

Originally developed by [Tim Franklin](https://github.com/franklin113/). Forked and added onto by me.