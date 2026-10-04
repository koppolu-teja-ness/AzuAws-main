// Placeholder handler for the migrated Azure Function App (migration demo).
exports.handler = async (event) => {
  return {
    statusCode: 200,
    body: JSON.stringify({ message: "Hello from the migrated Lambda function!" }),
  };
};
