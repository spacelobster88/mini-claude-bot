// SendReportEmail — JXA (JavaScript for Automation)
// Reads email params from pending_email.json, sends via Mail.app, deletes queue file.
// Supports reply threading: if reply_to_subject is set, finds the original message
// in Sent mailbox and replies to it.

ObjC.import("Foundation");

function run() {
  const queuePath =
    "/Users/spacelobster/Projects/mini-claude-bot/reports/output/pending_email.json";

  // Read queue file
  const fm = $.NSFileManager.defaultManager;
  if (!fm.fileExistsAtPath(queuePath)) {
    console.log("No pending email queue file found");
    return;
  }

  const data = $.NSData.dataWithContentsOfFile(queuePath);
  const str = $.NSString.alloc.initWithDataEncoding(data, $.NSUTF8StringEncoding).js;
  const params = JSON.parse(str);

  // Send via Mail.app
  const Mail = Application("Mail");
  Mail.activate();
  delay(2);

  let msg;

  if (params.reply_to_subject) {
    // Find the original message in Sent mailbox to reply to
    let originalMsg = null;
    const accounts = Mail.accounts();
    for (let i = 0; i < accounts.length; i++) {
      try {
        const sentBox = accounts[i].mailboxes.whose({ name: { _contains: "Sent" } })();
        for (let j = 0; j < sentBox.length; j++) {
          const messages = sentBox[j].messages.whose({ subject: params.reply_to_subject })();
          if (messages.length > 0) {
            originalMsg = messages[0];
            break;
          }
        }
      } catch (e) {
        // Skip accounts without Sent mailbox
      }
      if (originalMsg) break;
    }

    if (originalMsg) {
      // Reply to the original message
      msg = Mail.reply(originalMsg, { openingWindow: true });
      delay(2);
      // Set the body (reply prepends to existing content)
      msg.content = params.body;
    } else {
      // Fallback: send as new message if original not found
      console.log("Original message not found, sending as new email");
      msg = Mail.OutgoingMessage({
        subject: params.subject,
        content: params.body,
        visible: true,
        sender: params.sender,
      });
      Mail.outgoingMessages.push(msg);
    }
  } else {
    // New email (not a reply)
    msg = Mail.OutgoingMessage({
      subject: params.subject,
      content: params.body,
      visible: true,
      sender: params.sender,
    });
    Mail.outgoingMessages.push(msg);
  }

  // Add recipients
  msg.toRecipients.push(Mail.Recipient({ address: params.to }));
  if (params.cc) {
    msg.ccRecipients.push(Mail.Recipient({ address: params.cc }));
  }
  if (params.bcc) {
    msg.bccRecipients.push(Mail.Recipient({ address: params.bcc }));
  }

  // Add attachment
  if (params.attachment) {
    const attachment = Mail.Attachment({
      fileName: Path(params.attachment),
    });
    msg.attachments.push(attachment);
  }

  delay(3);
  msg.send();
  delay(3);

  // Delete queue file to signal success
  fm.removeItemAtPathError(queuePath, null);
  console.log("Email sent to " + params.to);
}
