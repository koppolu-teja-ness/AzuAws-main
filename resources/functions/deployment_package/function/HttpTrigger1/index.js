// Classic (v3) programming model handler so Azure Portal inline editing stays available.
module.exports = async function (context, req) {
  context.res = {
    status: 200,
    body: { message: "Hello from the migrated Lambda function!" },
  };
};
