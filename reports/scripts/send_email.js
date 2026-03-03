// SendReportEmail — JXA (JavaScript for Automation)
// Reads email params from pending_email.json, sends via Mail.app, deletes queue file.

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

  const msg = Mail.OutgoingMessage({
    subject: params.subject,
    content: params.body,
    visible: true,
    sender: params.sender,
  });
  Mail.outgoingMessages.push(msg);

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
